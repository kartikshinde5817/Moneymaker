"""Find a company's own website when a source only gave us its name.

Job ads, tender notices and company registers name the business but rarely link
to it. Without a site there is nothing to audit and no email to find, so the
prospect used to be thrown away. This guesses the likely addresses (acme.co.uk,
acme.com ...), and accepts one only if BOTH the domain name and the page itself
clearly say it is that company. A wrong site would mean an email about a page
the prospect has never seen, so when in doubt it returns nothing.

No search engine is involved, so it costs no search quota.
"""

from __future__ import annotations

import re
import socket

from app.services.ownership import domain_belongs_to
from app.services.research.fetcher import fetch
from app.services.resolution import normalize_name

STOP = {
    "the", "and", "of", "ltd", "limited", "llc", "inc", "incorporated", "plc", "llp", "corp",
    "corporation", "company", "co", "group", "holdings", "pvt", "private", "gmbh", "bv", "pty",
    "services", "solutions", "uk", "usa", "international", "global", "partners", "trading",
}

TLDS = {
    "GB": ["co.uk", "com", "uk", "org.uk"], "US": ["com", "org", "net", "io", "co"],
    "IN": ["com", "in", "co.in", "org"], "AU": ["com.au", "com", "net.au"],
    "NZ": ["co.nz", "com", "nz"], "IE": ["ie", "com"], "AE": ["ae", "com"],
    "SG": ["com.sg", "com", "sg"], "ZA": ["co.za", "com"], "NL": ["nl", "com"],
    "FR": ["fr", "com"], "SE": ["se", "com"],
}
MAX_TRIES = 8


def _words(name: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", normalize_name(name).lower())
    return [w for w in raw if w not in STOP]


def candidates(name: str, country: str = "US") -> list[str]:
    words = _words(name)
    if not words:
        return []
    slugs = []
    full = "".join(words)
    if 4 <= len(full) <= 30:
        slugs.append(full)
    if len(words) >= 2:
        two = "".join(words[:2])
        if two != full and len(two) >= 5:
            slugs.append(two)
        if "-".join(words[:3]) not in slugs and len(words) <= 3:
            slugs.append("-".join(words))
    if len(words[0]) >= 6 and words[0] not in slugs:
        slugs.append(words[0])
    out = []
    for slug in slugs:
        for tld in TLDS.get((country or "US").upper(), ["com"])[:3]:
            out.append(f"{slug}.{tld}")
    return out[:MAX_TRIES]


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def _says_it_is(name: str, html: str) -> bool:
    """Does the page's own title or opening text name this company?"""
    words = _words(name)
    if not words:
        return False
    low = html[:60000].lower()
    title = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    head = (title.group(1) if title else "") + " " + " ".join(re.findall(r"<h1[^>]*>(.*?)</h1>", low, re.S))
    head = re.sub(r"<[^>]+>|[^a-z0-9 ]", " ", head)
    lead = words[0]
    if len(lead) >= 4 and lead in head:
        return len(words) < 2 or words[1] in low  # a second word anywhere on the page
    return False


def find_website(name: str, country: str = "US") -> str | None:
    for host in candidates(name, country):
        url = f"https://{host}/"
        if not domain_belongs_to(name, url) or not _resolves(host):
            continue
        res = fetch(url, timeout=6)
        if not (res.ok and res.text):
            continue
        if _says_it_is(name, res.text):
            return res.final_url or url
    return None
