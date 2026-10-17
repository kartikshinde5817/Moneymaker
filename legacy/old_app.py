"""
Money Maker - one-file client-hunting console.

Backend (FastAPI) + frontend (embedded HTML) run together in this single file.
Just run:  python main.py

Leads are sourced live from OpenStreetMap (no key needed); their websites are
scraped for contact emails. Add SMTP in Settings to send real email, optionally
a Google Places key and a Claude key.

IMPORTANT / legal note (read the README):
  - Cold EMAIL is the default channel. SMS/WhatsApp to Indian numbers needs
    TRAI/DLT registration and opt-in, so this tool does NOT cold-blast phones.
"""

import os
import re
import time
import json
import sqlite3
import smtplib
import datetime as dt
import threading
import webbrowser
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from contextlib import closing

import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

APP_NAME = "Money Maker"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rainmaker.db")
HOST = os.environ.get("RAINMAKER_HOST", "127.0.0.1")
PORT = int(os.environ.get("RAINMAKER_PORT", "8000"))

# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with closing(db()) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                project_type TEXT,
                location TEXT,
                keywords TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id INTEGER,
                business_name TEXT,
                category TEXT,
                phone TEXT,
                email TEXT,
                website TEXT,
                address TEXT,
                source TEXT,
                status TEXT DEFAULT 'new',
                subject TEXT,
                body TEXT,
                created_at TEXT,
                sent_at TEXT,
                FOREIGN KEY(campaign_id) REFERENCES campaigns(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS projects (
                name TEXT PRIMARY KEY,
                keywords TEXT
            );
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_type TEXT UNIQUE,
                subject TEXT,
                body TEXT
            );
            """
        )
        # seed default settings
        defaults = {
            "company_name": "Your Studio",
            "company_tagline": "We build websites and computer-vision tools that pay for themselves.",
            "portfolio_url": "https://your-portfolio.example.com",
            "sender_name": "Alex",
            "sender_email": "you@yourstudio.com",
            "google_places_key": "",
            "anthropic_key": "",
            "claude_model": "claude-sonnet-4-6",
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_user": "",
            "smtp_pass": "",
            "daily_send_cap": "40",
        }
        for k, v in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v)
            )
        for pname, kws in PROJECT_KEYWORDS.items():
            conn.execute("INSERT OR IGNORE INTO projects(name, keywords) VALUES (?,?)",
                         (pname, ", ".join(kws)))
        # seed templates
        seed_templates(conn)


def seed_templates(conn):
    tpls = {
        "Website development": {
            "subject": "A sharper website for {business_name}?",
            "body": (
                "Hi {business_name} team,\n\n"
                "I came across {business_name} in {location} and loved what you're doing. "
                "I build fast, modern websites for local businesses like yours - the kind that "
                "turn online searches into walk-ins and orders.\n\n"
                "If it's useful, here's a bit of my recent work: {portfolio_url}\n\n"
                "Would you be open to a quick chat this week?\n\n"
                "Warm regards,\n{sender_name}\n{company_name}"
            ),
        },
        "Computer vision / OpenCV": {
            "subject": "Automating visual inspection at {business_name}",
            "body": (
                "Hi {business_name} team,\n\n"
                "I work on computer-vision systems (OpenCV, defect detection, counting, "
                "quality inspection) for manufacturing and operations teams. Companies like "
                "{business_name} often have repetitive visual checks that a camera + model can "
                "handle faster and more consistently.\n\n"
                "A snapshot of past projects: {portfolio_url}\n\n"
                "Open to a 15-minute call to see if there's a fit?\n\n"
                "Best,\n{sender_name}\n{company_name}"
            ),
        },
    }
    for pt, t in tpls.items():
        conn.execute(
            "INSERT OR IGNORE INTO templates(project_type, subject, body) VALUES (?,?,?)",
            (pt, t["subject"], t["body"]),
        )


def get_settings():
    with closing(db()) as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


def save_settings(d: dict):
    with closing(db()) as conn, conn:
        for k, v in d.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, str(v)),
            )


# ----------------------------------------------------------------------------
# Project types -> search keyword mapping (drives the whole targeting)
# ----------------------------------------------------------------------------

PROJECT_KEYWORDS = {
    "Website development": ["cafe", "restaurant", "boutique", "gym", "dental clinic", "salon"],
    "Computer vision / OpenCV": [
        "manufacturing company", "packaging factory", "quality inspection",
        "warehouse automation", "food processing plant", "textile mill",
    ],
}

def project_keywords(name):
    with closing(db()) as conn:
        row = conn.execute("SELECT keywords FROM projects WHERE name=?", (name,)).fetchone()
    if row and row["keywords"].strip():
        return [k.strip() for k in row["keywords"].split(",") if k.strip()]
    return PROJECT_KEYWORDS.get(name, [name])


def project_names():
    with closing(db()) as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM projects ORDER BY rowid")]


# ----------------------------------------------------------------------------
# Real sourcing via OpenStreetMap (Nominatim + Overpass) - free, no key needed
# ----------------------------------------------------------------------------

UA = {"User-Agent": "MoneyMaker/1.0 (lead research tool)"}
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# keyword -> Overpass tag filters
OSM_FILTERS = {
    "cafe": ['["amenity"="cafe"]'],
    "restaurant": ['["amenity"="restaurant"]'],
    "boutique": ['["shop"~"boutique|clothes|fashion"]'],
    "gym": ['["leisure"="fitness_centre"]'],
    "dental clinic": ['["amenity"="dentist"]', '["healthcare"="dentist"]'],
    "salon": ['["shop"~"hairdresser|beauty"]'],
    "manufacturing company": ['["man_made"="works"]', '["industrial"~"factory|manufacturing"]',
                              '["name"~"manufactur|industries|industrial|engineering",i]["office"!~"."]'],
    "packaging factory": ['["name"~"packag",i]'],
    "quality inspection": ['["name"~"quality|inspection|testing",i]["office"~"company|.*"]'],
    "warehouse automation": ['["building"="warehouse"]["name"]', '["industrial"="warehouse"]'],
    "food processing plant": ['["name"~"food process|foods|dairy|bakery",i]["man_made"~"works"]',
                              '["craft"~"confectionery|brewery|bakery"]'],
    "textile mill": ['["name"~"textile|mill|garment|spinning",i]'],
}


def _geocode(location):
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=20,
                         params={"q": location, "format": "json", "limit": 1})
        res = r.json()
    except Exception as e:
        raise HTTPException(502, f"Geocoding failed: {e}")
    if not res:
        raise HTTPException(404, f"Could not find location '{location}'")
    s, n, w, e = (float(x) for x in res[0]["boundingbox"])  # south, north, west, east
    return s, w, n, e


def _overpass(query):
    last = None
    for attempt in range(1):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=UA, timeout=25)
                if r.status_code == 200:
                    return r.json().get("elements", [])
                last = f"{url} HTTP {r.status_code}"
            except Exception as e:
                last = f"{url} {e}"
        time.sleep(3)
    raise HTTPException(502, f"Overpass search failed: {last}")


def _osm_address(t):
    parts = [t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb"),
             t.get("addr:city"), t.get("addr:postcode")]
    return ", ".join(p for p in parts if p)


NOMINATIM_TERMS = {
    "manufacturing company": "industries", "packaging factory": "packaging",
    "quality inspection": "factory", "warehouse automation": "warehouse",
    "food processing plant": "food industries", "textile mill": "textile",
}


NON_BUSINESS_TYPES = {"bank", "atm", "suburb", "neighbourhood", "city", "town", "village",
                      "residential", "bus_stop", "station"}


def _nominatim_leads(keywords, location, count):
    """Fallback when Overpass is unavailable: Nominatim free-text search."""
    found = {}
    for kw in keywords:
        if len(found) >= count * 3:
            break
        try:
            res = requests.get("https://nominatim.openstreetmap.org/search", headers=UA, timeout=25,
                               params={"q": f"{NOMINATIM_TERMS.get(kw, kw)} in {location}", "format": "json", "limit": 40,
                                       "extratags": 1, "addressdetails": 0}).json()
        except Exception:
            continue
        for p in res:
            t = p.get("extratags") or {}
            name = p.get("name")
            key = (p.get("osm_type"), p.get("osm_id"))
            if p.get("type") in NON_BUSINESS_TYPES or p.get("class") in ("place", "highway", "boundary"):
                continue
            if not name or key in found:
                continue
            found[key] = {
                "business_name": name,
                "category": kw,
                "phone": t.get("phone") or t.get("contact:phone") or "",
                "email": t.get("email") or t.get("contact:email") or "",
                "website": t.get("website") or t.get("contact:website") or "",
                "address": (p.get("display_name") or location)[:160],
                "source": "openstreetmap",
            }
        time.sleep(1.1)  # Nominatim usage policy: max 1 req/sec
    return found


def osm_leads(project_type, location, count):
    keywords = project_keywords(project_type)
    south, west, north, east = _geocode(location)
    bbox = f"{south},{west},{north},{east}"
    found = {}
    last_err = None
    for kw in keywords:
        if len(found) >= count * 3:
            break
        filters = OSM_FILTERS.get(kw) or ['["name"~"%s",i]' % re.sub(r'["\\]', "", re.escape(kw))]
        body = "".join(f"nwr{f}({bbox});" for f in filters)
        try:
            elements = _overpass(f"[out:json][timeout:20];({body});out center tags 150;")
        except HTTPException as e:
            last_err = e
            break  # Overpass is down/overloaded - fall back to Nominatim
        for el in elements:
            t = el.get("tags", {})
            name = t.get("name")
            key = (el["type"], el["id"])
            if not name or key in found:
                continue
            found[key] = {
                "business_name": name,
                "category": kw,
                "phone": t.get("phone") or t.get("contact:phone") or "",
                "email": t.get("email") or t.get("contact:email") or "",
                "website": t.get("website") or t.get("contact:website") or "",
                "address": _osm_address(t) or location,
                "source": "openstreetmap",
            }
    if not found:
        found = _nominatim_leads(keywords, location, count)
    if not found and last_err:
        raise last_err
    # best leads first: those with the most contact info
    ranked = sorted(found.values(),
                    key=lambda l: -(bool(l["email"]) * 3 + bool(l["website"]) * 2 + bool(l["phone"])))
    return ranked[:count * 3]


# ----------------------------------------------------------------------------
# Real Google Places sourcing (used when a key is set)
# ----------------------------------------------------------------------------

def google_places_leads(project_type, location, count, api_key):
    cats = project_keywords(project_type)
    collected = {}
    for kw in cats:
        if len(collected) >= count:
            break
        query = f"{kw} in {location}"
        try:
            r = requests.get(
                "https://maps.googleapis.com/maps/api/place/textsearch/json",
                params={"query": query, "key": api_key}, timeout=20,
            )
            data = r.json()
        except Exception as e:
            raise HTTPException(502, f"Places search failed: {e}")
        if data.get("status") not in ("OK", "ZERO_RESULTS"):
            raise HTTPException(502, f"Places API error: {data.get('status')} {data.get('error_message','')}")
        for place in data.get("results", []):
            pid = place.get("place_id")
            if not pid or pid in collected:
                continue
            phone, website = "", ""
            try:
                d = requests.get(
                    "https://maps.googleapis.com/maps/api/place/details/json",
                    params={"place_id": pid,
                            "fields": "formatted_phone_number,website,formatted_address",
                            "key": api_key}, timeout=20,
                ).json().get("result", {})
                phone = d.get("formatted_phone_number", "")
                website = d.get("website", "")
                addr = d.get("formatted_address", place.get("formatted_address", ""))
            except Exception:
                addr = place.get("formatted_address", "")
            collected[pid] = {
                "business_name": place.get("name", ""),
                "category": kw,
                "phone": phone,
                "email": "",  # filled by enrichment
                "website": website,
                "address": addr,
                "source": "google_places",
            }
            if len(collected) >= count:
                break
    return list(collected.values())[:count]


# ----------------------------------------------------------------------------
# Website validation: only keep sites that really respond
# ----------------------------------------------------------------------------

HOST_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}$")
BROWSER_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}


def normalize_url(u):
    """Fix common typos (http//x) and return a well-formed URL, or '' if not a real address."""
    from urllib.parse import urlparse
    u = (u or "").strip().split(";")[0].strip()
    u = re.sub(r"^(https?)//", r"\1://", u, flags=re.I)
    if not u or " " in u:
        return ""
    if not re.match(r"^https?://", u, re.I):
        u = "https://" + u
    p = urlparse(u)
    host = (p.hostname or "").lower()
    if not HOST_RE.match(host):
        return ""
    return u


def verify_website(u):
    """Return the working URL if the site responds, else ''."""
    u = normalize_url(u)
    if not u:
        return ""
    candidates = [u]
    if u.startswith("https://"):
        candidates.append("http://" + u[len("https://"):])
    for c in candidates:
        try:
            r = requests.get(c, headers=BROWSER_UA, timeout=8, allow_redirects=True, stream=True)
            r.close()
            # 401/403/429 = server is alive but blocks bots; 404/410/5xx = dead
            if r.status_code < 400 or r.status_code in (401, 403, 429):
                return c
        except Exception:
            continue
    return ""


def verify_leads(leads):
    """Validate every lead's website in parallel; drop dead ones and duplicate businesses."""
    from concurrent.futures import ThreadPoolExecutor
    seen, unique = set(), []
    for l in leads:
        k = re.sub(r"[^a-z0-9]", "", (l["business_name"] or "").lower())
        if k and k not in seen:
            seen.add(k)
            unique.append(l)
    with ThreadPoolExecutor(max_workers=10) as ex:
        sites = list(ex.map(lambda l: verify_website(l["website"]) if l.get("website") else "", unique))
    for l, site in zip(unique, sites):
        l["website"] = site
    return unique


# ----------------------------------------------------------------------------
# Enrichment: best-effort email from a business's OWN website
# ----------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def find_email_on_site(website):
    if not website:
        return ""
    urls = [website.rstrip("/"), website.rstrip("/") + "/contact",
            website.rstrip("/") + "/contact-us"]
    for u in urls:
        try:
            r = requests.get(u, timeout=10, headers={"User-Agent": "Mozilla/5.0 MoneyMakerBot"})
            found = EMAIL_RE.findall(r.text)
            found = [e for e in found if not e.lower().endswith((".png", ".jpg", ".gif", ".webp"))]
            if found:
                # prefer info@/contact@/sales@
                for pref in ("info", "contact", "hello", "sales", "enquiry"):
                    for e in found:
                        if e.lower().startswith(pref):
                            return e
                return found[0]
        except Exception:
            continue
    return ""


# ----------------------------------------------------------------------------
# Personalization: Claude API if key set, else template fill
# ----------------------------------------------------------------------------

def fill_template(text, lead, s):
    return (text
            .replace("{business_name}", lead["business_name"] or "there")
            .replace("{location}", lead["address"] or "")
            .replace("{category}", lead["category"] or "")
            .replace("{portfolio_url}", s.get("portfolio_url", ""))
            .replace("{sender_name}", s.get("sender_name", ""))
            .replace("{company_name}", s.get("company_name", "")))


def get_template(project_type):
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT subject, body FROM templates WHERE project_type=?", (project_type,)
        ).fetchone()
    if row:
        return row["subject"], row["body"]
    return ("Working together?", "Hi {business_name},\n\n{portfolio_url}\n\n{sender_name}")


def draft_message(lead, project_type, s):
    subj_t, body_t = get_template(project_type)
    base_subject = fill_template(subj_t, lead, s)
    base_body = fill_template(body_t, lead, s)

    key = s.get("anthropic_key", "").strip()
    if not key:
        return base_subject, base_body  # template mode

    prompt = (
        f"You are writing a short, warm, non-spammy cold outreach email.\n"
        f"Sender: {s.get('sender_name')} from {s.get('company_name')}.\n"
        f"Service offered: {project_type}. Portfolio: {s.get('portfolio_url')}.\n"
        f"Recipient business: {lead['business_name']} ({lead['category']}) in {lead['address']}.\n"
        f"Write a subject line and a 90-130 word email body. Friendly, specific, one clear ask "
        f"(a quick call). Include the portfolio link once. No emojis, no hype.\n"
        f'Respond ONLY as JSON: {{"subject": "...", "body": "..."}}'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": s.get("claude_model", "claude-sonnet-4-6"),
                  "max_tokens": 600,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40,
        )
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = text.strip().replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)
        return parsed.get("subject", base_subject), parsed.get("body", base_body)
    except Exception:
        return base_subject, base_body  # graceful fallback


# ----------------------------------------------------------------------------
# Sending: real SMTP
# ----------------------------------------------------------------------------

def send_email(to_email, subject, body, s):
    host = s.get("smtp_host", "").strip()
    if not host:
        raise HTTPException(400, "SMTP is not configured. Add your SMTP details in Settings.")
    if not to_email:
        raise HTTPException(400, "No email address for this lead")
    msg = MIMEMultipart()
    msg["From"] = f"{s.get('sender_name')} <{s.get('sender_email')}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP(host, int(s.get("smtp_port", "587")), timeout=30) as server:
            server.starttls()
            server.login(s.get("smtp_user"), s.get("smtp_pass"))
            server.sendmail(s.get("sender_email"), [to_email], msg.as_string())
    except Exception as e:
        raise HTTPException(502, f"Email send failed: {e}")
    return "sent"


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------

app = FastAPI(title=APP_NAME)


class RunReq(BaseModel):
    project_type: str
    location: str
    count: int = 10


class SettingsReq(BaseModel):
    values: dict


class TemplateReq(BaseModel):
    project_type: str
    subject: str
    body: str


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse(FRONTEND_HTML)


@app.get("/api/meta")
def meta():
    s = get_settings()
    return {"app": APP_NAME,
            "live_email": bool(s.get("smtp_host")),
            "ai_writer": bool(s.get("anthropic_key")),
            "project_types": project_names()}


@app.get("/api/stats")
def stats():
    with closing(db()) as conn:
        total = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        withmail = conn.execute("SELECT COUNT(*) c FROM leads WHERE email!=''").fetchone()["c"]
        sent = conn.execute("SELECT COUNT(*) c FROM leads WHERE status IN ('sent','opened','replied')").fetchone()["c"]
        replied = conn.execute("SELECT COUNT(*) c FROM leads WHERE status='replied'").fetchone()["c"]
    return {"total": total, "with_email": withmail, "sent": sent, "replied": replied}


@app.post("/api/run")
def run_pipeline(req: RunReq):
    s = get_settings()
    key = s.get("google_places_key", "").strip()
    if key:
        leads = google_places_leads(req.project_type, req.location, req.count, key)
    else:
        leads = osm_leads(req.project_type, req.location, req.count)
    leads = verify_leads(leads)
    leads.sort(key=lambda l: -(bool(l.get("email")) * 3 + bool(l.get("website")) * 2 + bool(l.get("phone"))))
    leads = leads[:req.count]
    if not leads:
        raise HTTPException(404, f"No businesses found for '{req.project_type}' in '{req.location}'. Try a larger area.")

    now = dt.datetime.now().isoformat(timespec="seconds")
    with closing(db()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO campaigns(name, project_type, location, keywords, created_at) VALUES (?,?,?,?,?)",
            (f"{req.project_type} - {req.location}", req.project_type, req.location,
             ",".join(project_keywords(req.project_type)), now),
        )
        cid = cur.lastrowid
        inserted = 0
        for ld in leads:
            # enrichment: try to find email if missing and site exists
            if not ld.get("email") and ld.get("website"):
                ld["email"] = find_email_on_site(ld["website"])
            conn.execute(
                "INSERT INTO leads(campaign_id,business_name,category,phone,email,website,address,source,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid, ld["business_name"], ld["category"], ld["phone"], ld.get("email", ""),
                 ld.get("website", ""), ld.get("address", ""), ld.get("source", ""), "new", now),
            )
            inserted += 1
    return {"campaign_id": cid, "leads_added": inserted}


class ProjectReq(BaseModel):
    name: str
    keywords: str = ""


@app.post("/api/projects")
def add_project(req: ProjectReq):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Project name is required")
    kws = ", ".join(k.strip() for k in (req.keywords or name).split(",") if k.strip())
    with closing(db()) as conn, conn:
        if conn.execute("SELECT 1 FROM projects WHERE name=? COLLATE NOCASE", (name,)).fetchone():
            raise HTTPException(409, "A project with this name already exists")
        conn.execute("INSERT INTO projects(name, keywords) VALUES (?,?)", (name, kws))
        conn.execute(
            "INSERT OR IGNORE INTO templates(project_type, subject, body) VALUES (?,?,?)",
            (name, "Working together on " + name.lower() + "?",
             "Hi {business_name} team,\n\nI came across {business_name} in {location} and think my "
             + name.lower() + " work could help.\n\nPortfolio: {portfolio_url}\n\n"
             "Would you be open to a quick chat this week?\n\nBest,\n{sender_name}\n{company_name}"))
    return {"ok": True, "name": name}


@app.delete("/api/projects")
def delete_project(name: str):
    with closing(db()) as conn, conn:
        if not conn.execute("SELECT 1 FROM projects WHERE name=?", (name,)).fetchone():
            raise HTTPException(404, "Project not found")
        n = conn.execute(
            "SELECT COUNT(*) c FROM leads WHERE campaign_id IN (SELECT id FROM campaigns WHERE project_type=?)",
            (name,)).fetchone()["c"]
        conn.execute("DELETE FROM campaigns WHERE project_type=?", (name,))  # cascades to leads
        conn.execute("DELETE FROM templates WHERE project_type=?", (name,))
        conn.execute("DELETE FROM projects WHERE name=?", (name,))
    return {"ok": True, "leads_deleted": n}


@app.get("/api/leads")
def list_leads(project_type: str = "", location: str = "", date: str = "", order: str = "desc"):
    asc = order == "asc"
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT l.*, c.project_type, c.location FROM leads l "
            "LEFT JOIN campaigns c ON l.campaign_id=c.id ORDER BY l.created_at, l.id"
        ).fetchall()
    all_leads = [dict(r) for r in rows]
    projects = sorted({l["project_type"] for l in all_leads if l["project_type"]})
    locations = sorted({l["location"] for l in all_leads if l["location"]})
    base = [l for l in all_leads
            if (not location or l["location"] == location)
            and (not date or (l["created_at"] or "")[:10] == date)]
    project_counts = {}
    for l in base:
        project_counts[l["project_type"]] = project_counts.get(l["project_type"], 0) + 1
    leads = [l for l in base if not project_type or l["project_type"] == project_type]
    if not asc:
        leads.reverse()
    groups = {}
    for l in leads:
        groups.setdefault((l["created_at"] or "")[:10], []).append(l)
    ordered = [{"day": d, "leads": groups[d]} for d in sorted(groups.keys(), reverse=not asc)]
    return {"days": ordered, "count": len(leads), "projects": projects, "locations": locations,
            "project_counts": project_counts, "total_all": len(base)}


@app.post("/api/leads/{lead_id}/draft")
def draft_one(lead_id: int):
    s = get_settings()
    with closing(db()) as conn, conn:
        row = conn.execute(
            "SELECT l.*, c.project_type FROM leads l LEFT JOIN campaigns c ON l.campaign_id=c.id WHERE l.id=?",
            (lead_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Lead not found")
        lead = dict(row)
        subject, body = draft_message(lead, lead.get("project_type") or "Website development", s)
        conn.execute("UPDATE leads SET subject=?, body=?, status=? WHERE id=?",
                     (subject, body, "drafted" if lead["status"] == "new" else lead["status"], lead_id))
    return {"subject": subject, "body": body}


@app.post("/api/leads/{lead_id}/send")
def send_one(lead_id: int):
    s = get_settings()
    with closing(db()) as conn, conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Lead not found")
        lead = dict(row)
        if not lead["email"]:
            raise HTTPException(400, "No email address for this lead")
        subject = lead["subject"] or "Hello"
        body = lead["body"] or ""
        if not lead["subject"]:
            with closing(db()) as c2:
                pt = c2.execute("SELECT project_type FROM campaigns WHERE id=?", (lead["campaign_id"],)).fetchone()
            subject, body = draft_message(lead, (pt["project_type"] if pt else "Website development"), s)
        result = send_email(lead["email"], subject, body, s)
        now = dt.datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE leads SET status='sent', subject=?, body=?, sent_at=? WHERE id=?",
                     (subject, body, now, lead_id))
    return {"result": result}


@app.post("/api/campaigns/{campaign_id}/blast")
def blast(campaign_id: int, confirm: bool = False):
    s = get_settings()
    cap = int(s.get("daily_send_cap", "40"))
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE campaign_id=? AND email!='' AND status IN ('new','drafted') LIMIT ?",
            (campaign_id, cap)).fetchall()
    sent, failed = 0, 0
    for r in rows:
        try:
            send_one(r["id"])
            sent += 1
        except HTTPException:
            failed += 1
    return {"sent": sent, "failed": failed}


@app.get("/api/settings")
def read_settings():
    s = get_settings()
    # mask secrets
    masked = dict(s)
    for k in ("anthropic_key", "google_places_key", "smtp_pass"):
        if masked.get(k):
            masked[k] = "••••••••"
    return masked


@app.post("/api/settings")
def write_settings(req: SettingsReq):
    clean = {k: v for k, v in req.values.items() if v != "••••••••"}
    save_settings(clean)
    return {"ok": True}


@app.get("/api/templates")
def list_templates():
    with closing(db()) as conn:
        rows = conn.execute("SELECT * FROM templates ORDER BY project_type").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/templates")
def save_template(req: TemplateReq):
    with closing(db()) as conn, conn:
        conn.execute(
            "INSERT INTO templates(project_type, subject, body) VALUES (?,?,?) "
            "ON CONFLICT(project_type) DO UPDATE SET subject=excluded.subject, body=excluded.body",
            (req.project_type, req.subject, req.body))
    return {"ok": True}


@app.delete("/api/leads")
def clear_leads():
    with closing(db()) as conn, conn:
        conn.execute("DELETE FROM leads")
        conn.execute("DELETE FROM campaigns")
    return {"ok": True}


@app.get("/api/export/excel")
def export_excel():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from io import BytesIO

    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT l.*, c.project_type, c.location FROM leads l "
            "LEFT JOIN campaigns c ON l.campaign_id=c.id ORDER BY l.created_at").fetchall()
    leads = [dict(r) for r in rows]

    wb = Workbook()
    wb.remove(wb.active)
    days = {}
    for l in leads:
        day = (l["created_at"] or "unknown")[:10]
        days.setdefault(day, []).append(l)

    headers = ["Business", "Category", "Project", "Location", "Phone", "Email",
               "Website", "Address", "Status", "Sent at"]
    header_fill = PatternFill("solid", fgColor="0E7490")
    header_font = Font(color="FFFFFF", bold=True)

    if not days:
        ws = wb.create_sheet("No data")
        ws["A1"] = "No leads yet - run a search first."
    for day in sorted(days.keys()):
        ws = wb.create_sheet(day[:31] or "day")
        for ci, h in enumerate(headers, 1):
            c = ws.cell(row=1, column=ci, value=h)
            c.fill = header_fill
            c.font = header_font
            c.alignment = Alignment(horizontal="left")
        for ri, l in enumerate(days[day], 2):
            vals = [l["business_name"], l["category"], l.get("project_type", ""),
                    l.get("location", ""), l["phone"], l["email"], l["website"],
                    l["address"], l["status"], l.get("sent_at", "")]
            for ci, v in enumerate(vals, 1):
                ws.cell(row=ri, column=ci, value=v)
        widths = [26, 20, 22, 16, 18, 30, 30, 34, 12, 20]
        for ci, w in enumerate(widths, 1):
            ws.column_dimensions[ws.cell(row=1, column=ci).column_letter].width = w
        ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"moneymaker_leads_{dt.date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ----------------------------------------------------------------------------
# Frontend (embedded) - served at /
# ----------------------------------------------------------------------------

FRONTEND_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Money Maker</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --ink:#16202E; --sky:#E7EDF2; --panel:#FFFFFF; --panel-2:#F5F8FA;
  --rain:#0E7490; --rain-bright:#06B6D4; --sun:#F59E0B; --drizzle:#93C5D8;
  --storm:#15202E; --storm-2:#1D2A3A; --line:#D7E0E8; --muted:#5C6B7A;
  --good:#0E9F6E; --warn:#E4572E;
  --r:12px; --r-sm:8px;
  box-sizing:border-box; padding-top:env(safe-area-inset-top,0px);
}
*{box-sizing:border-box}
html,body{margin:0; height:100%}
body{font-family:Inter,system-ui,sans-serif; color:var(--ink); background:var(--sky);
  display:flex; min-height:100%}
button{font-family:inherit; cursor:pointer}
input,select,textarea{font-family:inherit}
a{color:var(--rain)}

/* Sidebar */
.side{width:230px; background:var(--storm); color:#CDD9E3; display:flex; flex-direction:column;
  padding:22px 16px; position:sticky; top:0; height:100vh; flex-shrink:0}
.brand{display:flex; align-items:center; gap:11px; padding:0 6px 22px}
.brand svg{flex-shrink:0}
.brand h1{font-family:'Space Grotesk',sans-serif; font-size:21px; font-weight:600;
  color:#fff; margin:0; letter-spacing:-.5px}
.brand span{display:block; font-size:11px; color:#6E8090; font-weight:500; margin-top:1px}
.nav{display:flex; flex-direction:column; gap:3px; margin-top:6px}
.nav button{display:flex; align-items:center; gap:11px; width:100%; text-align:left;
  background:none; border:0; color:#9DB0BF; padding:10px 12px; border-radius:var(--r-sm);
  font-size:14px; font-weight:500; transition:background .12s,color .12s}
.nav button:hover{background:var(--storm-2); color:#E4EDF3}
.nav button.active{background:var(--rain); color:#fff}
.nav button svg{width:17px; height:17px; opacity:.9}
.side-foot{margin-top:auto; padding:12px 8px 0; font-size:12px; color:#5F7080; line-height:1.5}
.mode-chip{display:inline-flex; align-items:center; gap:6px; background:var(--storm-2);
  color:var(--sun); padding:5px 10px; border-radius:20px; font-size:11px; font-weight:600; margin-bottom:8px}
.mode-chip.live{color:var(--rain-bright)}
.dot{width:6px; height:6px; border-radius:50%; background:currentColor}

/* Main */
.main{flex:1; min-width:0; display:flex; flex-direction:column; height:100vh; overflow:hidden}
.view{flex:1; overflow-y:auto; padding:26px 30px 60px}
.view h2{font-family:'Space Grotesk',sans-serif; font-size:22px; font-weight:600;
  margin:0 0 3px; letter-spacing:-.4px}
.view .sub{color:var(--muted); font-size:13.5px; margin:0 0 22px}

/* Mission control */
.control{background:linear-gradient(135deg,var(--storm),var(--storm-2));
  border-radius:16px; padding:22px 24px; color:#fff; margin-bottom:22px;
  box-shadow:0 10px 30px -12px rgba(14,116,144,.5)}
.control-title{font-family:'Space Grotesk',sans-serif; font-size:15px; font-weight:500;
  color:#9DB8C6; margin:0 0 15px}
.control-row{display:flex; gap:14px; align-items:flex-end; flex-wrap:wrap}
.field{display:flex; flex-direction:column; gap:6px}
.field label{font-size:12px; color:#8FA6B5; font-weight:500}
.field input,.field select{background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.15);
  color:#fff; padding:11px 13px; border-radius:var(--r-sm); font-size:14px; min-width:200px}
.field input::placeholder{color:#8296A6}
.field select option{color:#16202E}
.field.count input{min-width:90px}
.run-btn{background:var(--rain-bright); color:#062A33; border:0; padding:12px 26px;
  border-radius:var(--r-sm); font-weight:700; font-size:14px; letter-spacing:.2px;
  display:inline-flex; align-items:center; gap:8px; transition:transform .1s,filter .12s}
.run-btn:hover{filter:brightness(1.07)}
.run-btn:active{transform:translateY(1px)}
.run-btn:disabled{opacity:.6; cursor:wait}

/* Stat tiles */
.tiles{display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:24px}
.tile{background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
  padding:16px 18px}
.tile .k{font-size:12px; color:var(--muted); font-weight:600; display:flex; align-items:center; gap:7px}
.tile .k .swatch{width:9px; height:9px; border-radius:3px}
.tile .v{font-family:'Space Grotesk',sans-serif; font-size:30px; font-weight:600;
  margin-top:8px; letter-spacing:-1px}

/* Table / day groups */
.toolbar{display:flex; justify-content:space-between; align-items:center; margin-bottom:14px; gap:12px; flex-wrap:wrap}
.toolbar .actions{display:flex; gap:9px}
.btn{background:var(--panel); border:1px solid var(--line); color:var(--ink);
  padding:9px 15px; border-radius:var(--r-sm); font-size:13px; font-weight:600;
  display:inline-flex; align-items:center; gap:7px; transition:border-color .12s,background .12s}
.btn:hover{border-color:var(--rain); background:var(--panel-2)}
.btn.primary{background:var(--rain); color:#fff; border-color:var(--rain)}
.btn.primary:hover{filter:brightness(1.06); background:var(--rain)}
.btn.ghost{background:none; border-color:transparent; color:var(--muted)}
.btn.danger:hover{border-color:var(--warn); color:var(--warn)}

.chips{display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px}
.chip{background:var(--panel); border:1px solid var(--line); color:var(--ink); padding:7px 14px;
  border-radius:20px; font-size:13px; font-weight:600; display:inline-flex; gap:8px; align-items:center}
.chip:hover{border-color:var(--rain)}
.chip.on{background:var(--rain); border-color:var(--rain); color:#fff}
.chip b{font-weight:600; opacity:.75; font-size:12px}
.ptag{display:inline-block; background:#E1F1F6; color:var(--rain); font-size:12px; font-weight:600;
  padding:3px 9px; border-radius:6px}
input.btn{font-family:inherit}
.day-group{margin-bottom:26px}
.day-head{display:flex; align-items:center; gap:12px; margin-bottom:10px}
.day-head h3{font-family:'Space Grotesk',sans-serif; font-size:15px; margin:0; font-weight:600}
.day-head .badge{background:var(--panel); border:1px solid var(--line); color:var(--muted);
  font-size:12px; font-weight:600; padding:3px 10px; border-radius:20px}
.day-line{flex:1; height:1px; background:var(--line)}

.tbl-wrap{background:var(--panel); border:1px solid var(--line); border-radius:var(--r); overflow:hidden}
table{width:100%; border-collapse:collapse; font-size:13.5px}
thead th{background:var(--panel-2); text-align:left; padding:11px 14px; font-weight:600;
  color:var(--muted); font-size:12px; border-bottom:1px solid var(--line)}
tbody td{padding:12px 14px; border-bottom:1px solid var(--line); vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:var(--panel-2)}
.biz{font-weight:600}
.biz small{display:block; color:var(--muted); font-weight:400; font-size:12px; margin-top:2px}
.mono{font-variant-numeric:tabular-nums; color:var(--ink)}
.nomail{color:var(--muted); font-style:italic; font-size:12.5px}

/* status pills - precipitation scale */
.pill{display:inline-flex; align-items:center; gap:6px; padding:4px 10px; border-radius:20px;
  font-size:12px; font-weight:600}
.pill .d{width:6px; height:6px; border-radius:50%}
.pill.new{background:#EAF3F7; color:#3E6B7E} .pill.new .d{background:var(--drizzle)}
.pill.drafted{background:#E1F1F6; color:#0E7490} .pill.drafted .d{background:var(--rain)}
.pill.sent{background:#DEF3F5; color:#0B7285} .pill.sent .d{background:var(--rain-bright)}
.pill.opened{background:#E7F6EF; color:#0E7A54} .pill.opened .d{background:var(--good)}
.pill.replied{background:#FEF2DC; color:#B4740A} .pill.replied .d{background:var(--sun)}

.row-actions{display:flex; gap:6px; justify-content:flex-end}
.icon-btn{background:none; border:1px solid var(--line); border-radius:6px; padding:6px 9px;
  font-size:12px; font-weight:600; color:var(--muted)}
.icon-btn:hover{border-color:var(--rain); color:var(--rain)}

/* empty */
.empty{text-align:center; padding:70px 20px; color:var(--muted)}
.empty svg{opacity:.4; margin-bottom:14px}
.empty h3{font-family:'Space Grotesk',sans-serif; color:var(--ink); margin:0 0 6px; font-size:17px}
.empty p{margin:0; font-size:14px}

/* settings / forms */
.card{background:var(--panel); border:1px solid var(--line); border-radius:var(--r);
  padding:22px 24px; margin-bottom:18px; max-width:760px}
.card h3{font-family:'Space Grotesk',sans-serif; margin:0 0 4px; font-size:16px}
.card .hint{color:var(--muted); font-size:13px; margin:0 0 18px}
.form-grid{display:grid; grid-template-columns:1fr 1fr; gap:15px}
.form-grid.one{grid-template-columns:1fr}
.inp{display:flex; flex-direction:column; gap:6px}
.inp label{font-size:12.5px; font-weight:600; color:var(--ink)}
.inp input,.inp select,.inp textarea{border:1px solid var(--line); border-radius:var(--r-sm);
  padding:10px 12px; font-size:14px; background:var(--panel-2); color:var(--ink)}
.inp input:focus,.inp textarea:focus,.inp select:focus{outline:2px solid var(--rain-bright); outline-offset:0; background:#fff}
.inp textarea{min-height:150px; resize:vertical; line-height:1.5}
.inp .fine{font-size:11.5px; color:var(--muted)}
.save-row{margin-top:18px; display:flex; gap:10px; align-items:center}

/* modal */
.modal-bg{position:fixed; inset:0; background:rgba(21,32,46,.55); display:none;
  align-items:center; justify-content:center; padding:20px; z-index:50}
.modal-bg.show{display:flex}
.modal{background:var(--panel); border-radius:16px; width:640px; max-width:100%;
  max-height:88vh; overflow-y:auto; padding:26px 28px}
.modal h3{font-family:'Space Grotesk',sans-serif; margin:0 0 4px; font-size:18px}
.modal .to{color:var(--muted); font-size:13px; margin:0 0 18px}

/* toast */
.toast{position:fixed; bottom:22px; left:50%; transform:translateX(-50%) translateY(80px);
  background:var(--ink); color:#fff; padding:13px 20px; border-radius:10px; font-size:14px;
  font-weight:500; box-shadow:0 12px 30px -10px rgba(0,0,0,.4); opacity:0; transition:.3s; z-index:80}
.toast.show{transform:translateX(-50%) translateY(0); opacity:1}
.toast.warn{background:var(--warn)}
.spin{width:15px; height:15px; border:2px solid rgba(255,255,255,.35); border-top-color:#fff;
  border-radius:50%; animation:sp .7s linear infinite; display:inline-block}
@keyframes sp{to{transform:rotate(360deg)}}

@media (max-width:860px){
  body{flex-direction:column; height:auto}
  .side{width:100%; height:auto; position:static; flex-direction:row; flex-wrap:wrap; align-items:center; padding:14px}
  .brand{padding:0 12px 0 4px}
  .nav{flex-direction:row; flex-wrap:wrap; margin:0}
  .side-foot{display:none}
  .main{height:auto}
  .tiles{grid-template-columns:repeat(2,1fr)}
  .form-grid{grid-template-columns:1fr}
  .view{padding:18px}
  .field input,.field select{min-width:0; width:100%}
  .field{flex:1 1 140px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important; animation:none!important}}
</style>
</head>
<body>
<aside class="side">
  <div class="brand">
    <svg width="30" height="30" viewBox="0 0 32 32" fill="none">
      <path d="M9 19a6 6 0 0 1 .6-11.97A7 7 0 0 1 23 8.5a5 5 0 0 1-.5 10.5H9z" fill="#06B6D4" opacity=".9"/>
      <path d="M11 22l-1.5 4M16 22l-1.5 4M21 22l-1.5 4" stroke="#F59E0B" stroke-width="2" stroke-linecap="round"/>
    </svg>
    <div><h1>Money Maker</h1><span>client-hunting console</span></div>
  </div>
  <nav class="nav">
    <button data-view="pipeline" class="active">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18M6 12h12M10 18h4"/></svg>Pipeline</button>
    <button data-view="templates">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M8 9h8M8 13h5"/></svg>Message templates</button>
    <button data-view="settings">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/></svg>Settings &amp; keys</button>
  </nav>
  <div class="side-foot">
    <div id="modeChip"></div>
    Email-first outreach. SMS/WhatsApp needs opt-in + DLT in India.
  </div>
</aside>

<main class="main">
  <div class="view" id="view"></div>
</main>

<div class="modal-bg" id="modalBg">
  <div class="modal" id="modalBox"></div>
</div>
<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let META = {project_types:[]};
let LEADS = {};
let FILTER = {project:'', location:'', date:''};
const CITIES = ['Pune','Mumbai','Delhi','Bengaluru','Hyderabad','Chennai','Kolkata','Ahmedabad','Jaipur','Surat','Nagpur','Nashik','Indore','Lucknow','Chandigarh','Kochi'];

function toast(msg, warn){
  const t=$('#toast'); t.textContent=msg; t.className='toast show'+(warn?' warn':'');
  setTimeout(()=>t.className='toast',2600);
}
async function api(path, opts){
  const r = await fetch(path, opts);
  if(!r.ok){ let m='Error'; try{m=(await r.json()).detail||m}catch(e){} throw new Error(m); }
  return r.headers.get('content-type','').includes('json') ? r.json() : r;
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}

async function loadMeta(){
  META = await api('/api/meta');
  const chip = $('#modeChip');
  chip.innerHTML = META.live_email
    ? '<span class="mode-chip live"><span class="dot"></span>Email ready</span>'
    : '<span class="mode-chip"><span class="dot"></span>SMTP not set</span>';
}

// ---------- Pipeline view ----------
async function viewPipeline(){
  const stats = await api('/api/stats');
  const opts = META.project_types.map(p=>`<option>${esc(p)}</option>`).join('');
  $('#view').innerHTML = `
    <h2>Pipeline</h2>
    <p class="sub">Set a target, pull in leads, and send personalized outreach. Leads are grouped by the day you collected them.</p>

    <div class="control">
      <p class="control-title">Mission control</p>
      <div class="control-row">
        <div class="field"><label>Project type</label>
          <div style="display:flex; gap:8px">
            <select id="ptype"><option value="" ${META.project_types.length?'hidden':''}>Select a project…</option>${opts}<option value="__new">＋ Add new project…</option></select>
            <button type="button" class="btn" id="addProjBtn" title="Add new project" style="padding:0 14px">＋ New</button></div></div>
        <div class="field"><label>Company location (city / area)</label>
          <input id="loc" list="locList" placeholder="e.g. Pune" value="${esc(FILTER.location||'Pune')}">
          <datalist id="locList">${CITIES.map(c=>`<option value="${c}">`).join('')}</datalist></div>
        <div class="field count"><label>How many</label>
          <input id="cnt" type="number" value="10" min="1" max="60"></div>
        <button class="run-btn" id="runBtn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          Find leads</button>
      </div>
    </div>

    <div class="tiles">
      <div class="tile"><div class="k"><span class="swatch" style="background:var(--drizzle)"></span>Leads collected</div><div class="v">${stats.total}</div></div>
      <div class="tile"><div class="k"><span class="swatch" style="background:var(--rain)"></span>With email</div><div class="v">${stats.with_email}</div></div>
      <div class="tile"><div class="k"><span class="swatch" style="background:var(--rain-bright)"></span>Outreach sent</div><div class="v">${stats.sent}</div></div>
      <div class="tile"><div class="k"><span class="swatch" style="background:var(--sun)"></span>Replied</div><div class="v">${stats.replied}</div></div>
    </div>

    <div class="toolbar">
      <div></div>
      <div class="actions">
        <button class="btn" id="exportBtn">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>
          Export Excel (day-wise)</button>
        <button class="btn danger" id="clearBtn">Clear all</button>
      </div>
    </div>
    <div class="toolbar" id="filterBar"></div>
    <div id="leadArea"></div>`;
  const pt=$('#ptype');
  $('#addProjBtn').onclick=openAddProject;
  pt.value=FILTER.project||META.project_types[0]||'';
  pt.onchange=()=>{
    if(pt.value==='__new'){ pt.value=FILTER.project||META.project_types[0]||''; openAddProject(); return; }
    if(!pt.value) return;
    FILTER.project=pt.value; loadLeads();
  };

  $('#runBtn').onclick = runSearch;
  $('#exportBtn').onclick = ()=> window.location='/api/export/excel';
  $('#clearBtn').onclick = async ()=>{
    if(!confirm('Delete all leads and campaigns? This cannot be undone.')) return;
    await api('/api/leads',{method:'DELETE'}); toast('Cleared'); viewPipeline();
  };
  loadLeads();
}

async function runSearch(){
  const btn=$('#runBtn'); const orig=btn.innerHTML;
  btn.disabled=true; btn.innerHTML='<span class="spin"></span> Searching…';
  try{
    if(!$('#ptype').value){ toast('Add or select a project first',true); btn.disabled=false; btn.innerHTML=orig; return; }
    FILTER.project=$('#ptype').value; FILTER.location=$('#loc').value.trim()||'Pune';
    const body={project_type:$('#ptype').value, location:FILTER.location, count:parseInt($('#cnt').value)||10};
    const res=await api('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    toast(`Added ${res.leads_added} real leads`);
    viewPipeline();
  }catch(e){ toast(e.message,true); btn.disabled=false; btn.innerHTML=orig; }
}

function openAddProject(){
  const box=$('#modalBox'); $('#modalBg').classList.add('show');
  box.innerHTML=`<h3>Add new project</h3>
    <p class="to">It will appear in the project dropdown. Search terms decide which businesses get found.</p>
    <div class="inp" style="margin-bottom:14px"><label>Project name</label><input id="npName" placeholder="e.g. Mobile app development"></div>
    <div class="inp"><label>Search terms (comma separated)</label><input id="npKw" placeholder="e.g. hospital, clinic, pharmacy"><span class="fine">Kinds of businesses to look for. Defaults to the project name.</span></div>
    <div class="save-row"><button class="btn primary" onclick="saveProject()">Add project</button>
      <button class="btn ghost" onclick="closeModal()">Cancel</button></div>`;
}
async function saveProject(){
  try{
    const name=$('#npName').value.trim();
    await api('/api/projects',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name, keywords:$('#npKw').value})});
    FILTER.project=name; closeModal(); toast('Project added'); await loadMeta(); viewPipeline();
  }catch(e){ toast(e.message,true); }
}

async function loadLeads(){
  const q=new URLSearchParams({project_type:FILTER.project, location:FILTER.viewLoc||'', date:FILTER.date||''});
  const data = await api('/api/leads?'+q);
  LEADS={}; data.days.forEach(g=>g.leads.forEach(l=>LEADS[l.id]=l));
  const opt=(list,cur,all)=>`<option value="">${all}</option>`+list.map(x=>`<option ${x===cur?'selected':''}>${esc(x)}</option>`).join('');
  const chip=(name,label,n)=>`<button class="chip ${FILTER.project===name?'on':''}" data-p="${esc(name)}">${esc(label)} <b>${n}</b></button>`;
  $('#filterBar').innerHTML=`<div style="width:100%">
      <div class="chips">${chip('','All projects',data.total_all)}${META.project_types.map(p=>chip(p,p,data.project_counts[p]||0)).join('')}</div>
      <div class="actions" style="align-items:center">
        <select class="btn" id="fLoc">${opt(data.locations,FILTER.viewLoc||'','All locations')}</select>
        <label class="sub" style="margin:0">Generated on</label>
        <input type="date" class="btn" id="fDate" value="${FILTER.date||''}">
        ${FILTER.date?'<button class="btn ghost" id="fClear">Clear date</button>':''}
        ${FILTER.project?'<button class="btn danger" id="fDelProj">Delete project</button>':''}
        <span class="sub" style="margin:0">${data.count} leads</span>
      </div></div>`;
  document.querySelectorAll('.chip').forEach(c=>c.onclick=()=>{
    FILTER.project=c.dataset.p;
    const pt=$('#ptype'); if(pt && FILTER.project) pt.value=FILTER.project;
    loadLeads();
  });
  $('#fLoc').onchange=e=>{FILTER.viewLoc=e.target.value; loadLeads();};
  $('#fDate').onchange=e=>{FILTER.date=e.target.value; loadLeads();};
  if($('#fDelProj')) $('#fDelProj').onclick=async()=>{
    const p=FILTER.project, n=data.project_counts[p]||0;
    if(!confirm(`Delete project "${p}" and all its leads (${n} in current view)? This cannot be undone.`)) return;
    try{ const r=await api('/api/projects?name='+encodeURIComponent(p),{method:'DELETE'});
      FILTER.project=''; toast(`Project deleted (${r.leads_deleted} leads removed)`); await loadMeta(); viewPipeline();
    }catch(e){ toast(e.message,true); }
  };
  if($('#fClear')) $('#fClear').onclick=()=>{FILTER.date=''; loadLeads();};
  const area = $('#leadArea');
  if(!data.count){
    area.innerHTML = `<div class="empty">
      <svg width="46" height="46" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 18a6 6 0 0 1 .6-11.97A7 7 0 0 1 23 7.5a5 5 0 0 1-.5 10.5H9z"/><path d="M8 21l-1 2M13 21l-1 2"/></svg>
      <h3>No leads found</h3><p>${FILTER.date||FILTER.project||FILTER.viewLoc?'Nothing matches these filters. Try clearing them.':'Pick a project type and a location above, then hit “Find leads”.'}</p></div>`;
    return;
  }
  area.innerHTML = data.days.map(g=>`
    <div class="day-group">
      <div class="day-head"><h3>${fmtDay(g.day)}</h3><span class="badge">${g.leads.length} leads</span><div class="day-line"></div></div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Business</th><th>Project</th><th>Location</th><th>Phone</th><th>Email</th><th>Website</th><th>Generated</th><th>Status</th><th style="text-align:right">Action</th></tr></thead>
        <tbody>${g.leads.map(rowHtml).join('')}</tbody>
      </table></div>
    </div>`).join('');
}

function rowHtml(l){
  const mail = l.email ? `<span class="mono">${esc(l.email)}</span>` : `<span class="nomail">no email found</span>`;
  return `<tr>
    <td class="biz">${esc(l.business_name)}<small>${esc(l.category)}</small></td>
    <td><span class="ptag">${esc(l.project_type||'—')}</span></td>
    <td>${esc(l.address||l.location||'—')}</td>
    <td class="mono">${esc(l.phone||'—')}</td>
    <td>${mail}</td>
    <td>${l.website?`<button class="icon-btn" style="color:var(--good);border-color:var(--good)" onclick="openSite(${l.id})">Yes</button>`:'<span class="nomail">No</span>'}</td>
    <td class="mono">${fmtStamp(l.created_at)}</td>
    <td><span class="pill ${l.status}"><span class="d"></span>${l.status}</span></td>
    <td><div class="row-actions">
      <button class="icon-btn" onclick="openDraft(${l.id})">Preview</button>
      ${l.email?`<button class="icon-btn" onclick="quickSend(${l.id})">Send</button>`:''}
    </div></td>
  </tr>`;
}

function siteUrl(u){
  u=(u||'').trim(); if(!u) return '';
  if(!/^https?:\/\//i.test(u)) u='https://'+u;
  return /^https?:\/\//i.test(u)?u:'';
}
function openSite(id){
  const l=LEADS[id]; if(!l) return;
  const url=siteUrl(l.website);
  $('#modalBg').classList.add('show');
  $('#modalBox').innerHTML=`<h3>${esc(l.business_name)}</h3>
    <p class="to">${esc(l.project_type||'')} · ${esc(l.address||l.location||'')}</p>
    <div class="card" style="margin:0 0 16px;padding:16px 18px">
      <div class="fine" style="font-size:12px;color:var(--muted);margin-bottom:6px">Website</div>
      ${url?`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" style="font-weight:600;word-break:break-all">${esc(url)}</a>`:'<span class="nomail">Invalid website address</span>'}
      ${l.phone?`<div style="margin-top:12px;font-size:13px">Phone: <span class="mono">${esc(l.phone)}</span></div>`:''}
      ${l.email?`<div style="margin-top:4px;font-size:13px">Email: <span class="mono">${esc(l.email)}</span></div>`:''}
    </div>
    <div class="save-row">${url?`<a class="btn primary" href="${esc(url)}" target="_blank" rel="noopener noreferrer" style="text-decoration:none">Open website ↗</a>`:''}
      <button class="btn ghost" onclick="closeModal()">Close</button></div>`;
}

function fmtStamp(iso){
  if(!iso) return '—';
  const d=new Date(iso);
  return d.toLocaleDateString(undefined,{day:'numeric',month:'short',year:'numeric'})+'<br><small style="color:var(--muted)">'+
    d.toLocaleTimeString(undefined,{hour:'numeric',minute:'2-digit'})+'</small>';
}
function localDay(t){const d=new Date(t); return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');}

function fmtDay(d){
  if(!d) return 'Unknown day';
  const today=localDay(Date.now());
  const y=localDay(Date.now()-864e5);
  if(d===today) return 'Today';
  if(d===y) return 'Yesterday';
  return new Date(d+'T00:00:00').toLocaleDateString(undefined,{weekday:'short',day:'numeric',month:'short',year:'numeric'});
}

async function openDraft(id){
  const box=$('#modalBox'); $('#modalBg').classList.add('show');
  box.innerHTML='<p class="to">Writing message…</p>';
  try{
    const d=await api(`/api/leads/${id}/draft`,{method:'POST'});
    box.innerHTML=`
      <h3>Outreach preview</h3>
      <p class="to">${META.ai_writer?'Written by Claude':'From your template'} · edit before sending</p>
      <div class="inp" style="margin-bottom:14px"><label>Subject</label><input id="mSubj" value="${esc(d.subject)}"></div>
      <div class="inp"><label>Body</label><textarea id="mBody">${esc(d.body)}</textarea></div>
      <div class="save-row">
        <button class="btn primary" onclick="sendFromModal(${id})">Send</button>
        <button class="btn ghost" onclick="closeModal()">Close</button>
      </div>`;
  }catch(e){ box.innerHTML=`<p class="to">${e.message}</p><button class="btn" onclick="closeModal()">Close</button>`; }
}
async function sendFromModal(id){
  try{ const r=await api(`/api/leads/${id}/send`,{method:'POST'});
    toast('Email sent'); closeModal(); loadLeads();
  }catch(e){ toast(e.message,true); }
}
async function quickSend(id){
  try{ const r=await api(`/api/leads/${id}/send`,{method:'POST'});
    toast('Email sent'); loadLeads();
  }catch(e){ toast(e.message,true); }
}
function closeModal(){ $('#modalBg').classList.remove('show'); }
$('#modalBg').onclick=e=>{ if(e.target.id==='modalBg') closeModal(); };

// ---------- Templates view ----------
async function viewTemplates(){
  const tpls=await api('/api/templates');
  $('#view').innerHTML=`<h2>Message templates</h2>
    <p class="sub">One template per project type. Use placeholders like {business_name}, {location}, {portfolio_url}, {sender_name}, {company_name}. If a Claude key is set, these become the brief the AI writes from.</p>
    ${tpls.map((t,i)=>`
      <div class="card">
        <h3>${esc(t.project_type)}</h3>
        <div class="inp" style="margin-bottom:13px"><label>Subject</label><input id="ts${i}" value="${esc(t.subject)}"></div>
        <div class="inp"><label>Body</label><textarea id="tb${i}">${esc(t.body)}</textarea></div>
        <div class="save-row"><button class="btn primary" onclick="saveTpl(${i},'${esc(t.project_type)}')">Save template</button></div>
      </div>`).join('')}`;
}
async function saveTpl(i,pt){
  try{ await api('/api/templates',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({project_type:pt, subject:$('#ts'+i).value, body:$('#tb'+i).value})});
    toast('Template saved');
  }catch(e){ toast(e.message,true); }
}

// ---------- Settings view ----------
async function viewSettings(){
  const s=await api('/api/settings');
  const v=k=>esc(s[k]||'');
  $('#view').innerHTML=`<h2>Settings &amp; keys</h2>
    <p class="sub">Lead search works out of the box via OpenStreetMap. Add SMTP to send email. Keys stay on your machine in the local database.</p>

    <div class="card">
      <h3>Your company</h3>
      <p class="hint">This is what goes into every outreach message.</p>
      <div class="form-grid">
        <div class="inp"><label>Company name</label><input id="company_name" value="${v('company_name')}"></div>
        <div class="inp"><label>Sender name</label><input id="sender_name" value="${v('sender_name')}"></div>
        <div class="inp"><label>Sender email</label><input id="sender_email" value="${v('sender_email')}"></div>
        <div class="inp"><label>Portfolio URL</label><input id="portfolio_url" value="${v('portfolio_url')}"></div>
        <div class="inp" style="grid-column:1/-1"><label>Tagline</label><input id="company_tagline" value="${v('company_tagline')}"></div>
      </div>
    </div>

    <div class="card">
      <h3>Lead sourcing &amp; AI</h3>
      <p class="hint">Leads come from OpenStreetMap by default. Optionally add a Google Places key for broader coverage, and a Claude key to have outreach written for each lead.</p>
      <div class="form-grid">
        <div class="inp"><label>Google Places API key</label><input id="google_places_key" placeholder="AIza…" value="${v('google_places_key')}"><span class="fine">Sanctioned, paid per lookup. Avoids scraping-ToS issues.</span></div>
        <div class="inp"><label>Anthropic (Claude) API key</label><input id="anthropic_key" placeholder="sk-ant-…" value="${v('anthropic_key')}"></div>
        <div class="inp"><label>Claude model</label><input id="claude_model" value="${v('claude_model')}"></div>
      </div>
    </div>

    <div class="card">
      <h3>Email sending (SMTP)</h3>
      <p class="hint">Required to send. Use a real ESP (Amazon SES, SendGrid SMTP, etc.) with domain auth for deliverability — not your personal inbox.</p>
      <div class="form-grid">
        <div class="inp"><label>SMTP host</label><input id="smtp_host" placeholder="email-smtp.region.amazonaws.com" value="${v('smtp_host')}"></div>
        <div class="inp"><label>SMTP port</label><input id="smtp_port" value="${v('smtp_port')}"></div>
        <div class="inp"><label>SMTP username</label><input id="smtp_user" value="${v('smtp_user')}"></div>
        <div class="inp"><label>SMTP password</label><input id="smtp_pass" type="password" placeholder="${s.smtp_pass?'••••••••':''}" value=""></div>
        <div class="inp"><label>Daily send cap</label><input id="daily_send_cap" value="${v('daily_send_cap')}"><span class="fine">Protects your domain reputation.</span></div>
      </div>
      <div class="save-row"><button class="btn primary" id="saveSet">Save settings</button></div>
    </div>`;

  $('#saveSet').onclick=async()=>{
    const keys=['company_name','sender_name','sender_email','portfolio_url','company_tagline',
      'google_places_key','anthropic_key','claude_model','smtp_host','smtp_port','smtp_user','smtp_pass','daily_send_cap'];
    const values={};
    keys.forEach(k=>{ const el=$('#'+k); if(el && el.value!=='') values[k]=el.value; });
    try{ await api('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({values})});
      toast('Settings saved'); await loadMeta();
    }catch(e){ toast(e.message,true); }
  };
}

// ---------- Router ----------
const VIEWS={pipeline:viewPipeline, templates:viewTemplates, settings:viewSettings};
document.querySelectorAll('.nav button').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.nav button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active'); VIEWS[b.dataset.view]();
  };
});
(async()=>{ await loadMeta(); viewPipeline(); })();
</script>
</body>
</html>"""


# ----------------------------------------------------------------------------
# Launch: run backend + open the frontend in the browser (one command)
# ----------------------------------------------------------------------------

def open_browser():
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    import uvicorn
    init_db()
    print("\n" + "=" * 54)
    print(f"  {APP_NAME} is starting…")
    print(f"  Open:  http://{HOST}:{PORT}")
    print("  Leads are sourced live from OpenStreetMap.")
    print("=" * 54 + "\n")
    threading.Timer(1.2, open_browser).start()
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
