"""HTTP API and the public unsubscribe endpoint."""

from __future__ import annotations

import datetime as dt
import threading

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db, session_scope
from app.models import (
    AIAnalysis, Campaign, CampaignRun, Company, Contact, EmailEvent,
    EmailMessage, Lead, Mailbox, SearchQuery, Signal, Source, Suppression,
    WebsiteAudit, WhatsAppMessage, utcnow,
)
from app.services import ai, inbox, linkedin, mailer, notify, pipeline, scoring, whatsapp
from app.services.compliance import COUNTRY_POLICY, policy_for_country, suppress
from app.services.discovery import available_sources
from app.services.signals import company_signals

router = APIRouter(prefix="/api")

# Guards against two runs of the same campaign overlapping.
_running: set[int] = set()
_running_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Meta & stats
# ---------------------------------------------------------------------------


@router.get("/meta")
def meta(db: Session = Depends(get_db)):
    return {
        "app": "Money Maker",
        "ai_ready": ai.available(),
        "email_ready": mailer.provider_ready(),
        "sender_configured": bool(settings.sender_email and settings.company_postal_address),
        "sources": available_sources(),
        "services": [
            {"key": "web_development", "label": "Web Development"},
            {"key": "computer_vision", "label": "Computer Vision / OpenCV"},
            {"key": "ai_agent", "label": "AI Agents"},
            {"key": "ai_automation", "label": "AI Automation"},
        ],
        "country_policy": {
            code: {"policy": p, "reason": r} for code, (p, r) in COUNTRY_POLICY.items()
        },
        "scheduler_enabled": settings.scheduler_enabled,
        "timezone": settings.scheduler_timezone,
        "telegram_ready": notify.ready(),
        "inbox_ready": inbox.ready(),
        "whatsapp_enabled": settings.whatsapp_enabled,
    }


@router.get("/stats/summary")
def stats_summary(db: Session = Depends(get_db)):
    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    def count(model, *where):
        q = select(func.count(model.id))
        for w in where:
            q = q.where(w)
        return db.execute(q).scalar_one()

    cost = db.execute(select(func.coalesce(func.sum(AIAnalysis.cost_usd), 0.0))).scalar_one()
    return {
        "total_leads": count(Lead),
        "new_today": count(Lead, Lead.created_at >= today),
        "qualified": count(Lead, Lead.score >= scoring.MEDIUM_THRESHOLD, Lead.stage != "rejected"),
        "high_intent": count(Lead, Lead.intent_level == "high", Lead.stage != "rejected"),
        "with_email": count(Lead, Lead.contact_id.isnot(None)),
        "pending_approval": count(EmailMessage, EmailMessage.status == "pending_approval"),
        "queued": count(EmailMessage, EmailMessage.status == "queued"),
        "emails_sent": count(EmailMessage, EmailMessage.status == "sent"),
        "sent_today": count(EmailMessage, EmailMessage.status == "sent", EmailMessage.sent_at >= today),
        "replies": count(EmailEvent, EmailEvent.type == "reply"),
        "bounces": count(EmailEvent, EmailEvent.type == "bounce"),
        "unsubscribed": count(Suppression),
        "rejected": count(Lead, Lead.stage == "rejected"),
        "whatsapp_pending": count(WhatsAppMessage, WhatsAppMessage.status == "pending"),
        "interested": count(Lead, Lead.status == "interested"),
        "companies": count(Company),
        "ai_cost_usd": round(float(cost or 0), 4),
        "daily_cap": settings.daily_email_cap_global,
    }


@router.get("/stats/breakdown")
def stats_breakdown(by: str = "service", db: Session = Depends(get_db)):
    column = {
        "service": Lead.service_category,
        "source": Lead.source_kind,
        "intent": Lead.intent_level,
        "status": Lead.status,
        "stage": Lead.stage,
    }.get(by)
    if column is not None:
        rows = db.execute(
            select(column, func.count(Lead.id)).group_by(column).order_by(func.count(Lead.id).desc())
        ).all()
    elif by == "country":
        rows = db.execute(
            select(Company.country, func.count(Lead.id))
            .join(Lead, Lead.company_id == Company.id)
            .group_by(Company.country).order_by(func.count(Lead.id).desc())
        ).all()
    elif by == "industry":
        rows = db.execute(
            select(Company.industry, func.count(Lead.id))
            .join(Lead, Lead.company_id == Company.id)
            .group_by(Company.industry).order_by(func.count(Lead.id).desc()).limit(12)
        ).all()
    else:
        raise HTTPException(400, f"unknown breakdown '{by}'")
    return [{"key": k or "unknown", "count": c} for k, c in rows]


@router.get("/stats/timeseries")
def stats_timeseries(days: int = 30, db: Session = Depends(get_db)):
    since = utcnow() - dt.timedelta(days=days)
    leads = db.execute(
        select(func.date(Lead.created_at), func.count(Lead.id))
        .where(Lead.created_at >= since).group_by(func.date(Lead.created_at))
    ).all()
    sent = db.execute(
        select(func.date(EmailMessage.sent_at), func.count(EmailMessage.id))
        .where(EmailMessage.sent_at >= since).group_by(func.date(EmailMessage.sent_at))
    ).all()
    lead_map = {str(d): c for d, c in leads if d}
    sent_map = {str(d): c for d, c in sent if d}
    out = []
    for i in range(days, -1, -1):
        day = (utcnow() - dt.timedelta(days=i)).date().isoformat()
        out.append({"day": day, "leads": lead_map.get(day, 0), "sent": sent_map.get(day, 0)})
    return out


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


class CampaignIn(BaseModel):
    name: str
    service_category: str
    sources: list[str] = []
    keywords: list[str] = []
    locations: list[str] = []
    countries: list[str] = ["GB", "US"]
    schedule_time: str = "07:00"
    enabled: bool = True
    daily_lead_limit: int = 25
    daily_email_limit: int = 20
    approval_mode: str = "manual"
    min_score: int = 55
    rotate_cities: bool = True


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db)):
    out = []
    for c in db.execute(select(Campaign).order_by(Campaign.schedule_time)).scalars():
        last = db.execute(
            select(CampaignRun).where(CampaignRun.campaign_id == c.id)
            .order_by(CampaignRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()
        leads = db.execute(
            select(func.count(Lead.id)).where(Lead.campaign_id == c.id)
        ).scalar_one()
        out.append({
            "id": c.id, "name": c.name, "service_category": c.service_category,
            "enabled": c.enabled, "sources": c.sources, "keywords": c.keywords,
            "locations": c.locations, "countries": c.countries,
            "schedule_time": c.schedule_time, "min_score": c.min_score,
            "daily_lead_limit": c.daily_lead_limit,
            "daily_email_limit": c.daily_email_limit,
            "approval_mode": c.approval_mode, "total_leads": leads,
            "rotate_cities": c.rotate_cities,
            "city_coverage": __import__("app.services.cities", fromlist=["x"]).coverage(
                c.countries or [], c.city_history),
            "running": c.id in _running,
            "last_run": None if not last else {
                "id": last.id, "status": last.status,
                "started_at": last.started_at.isoformat() if last.started_at else None,
                "stats": last.stats,
            },
        })
    return out


@router.post("/campaigns")
def create_campaign(payload: CampaignIn, db: Session = Depends(get_db)):
    c = Campaign(**payload.model_dump())
    db.add(c)
    db.commit()
    return {"id": c.id}


@router.put("/campaigns/{campaign_id}")
def update_campaign(campaign_id: int, payload: CampaignIn, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    for k, v in payload.model_dump().items():
        setattr(c, k, v)
    db.commit()
    from app.scheduler import reschedule
    reschedule()
    return {"ok": True}


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int, db: Session = Depends(get_db)):
    c = db.get(Campaign, campaign_id)
    if not c:
        raise HTTPException(404, "campaign not found")
    db.delete(c)
    db.commit()
    from app.scheduler import reschedule
    reschedule()
    return {"ok": True}


def _run_campaign_bg(campaign_id: int, trigger: str) -> None:
    try:
        with session_scope() as db:
            campaign = db.get(Campaign, campaign_id)
            if campaign:
                pipeline.run_campaign(db, campaign, trigger=trigger)
    finally:
        with _running_lock:
            _running.discard(campaign_id)


@router.post("/campaigns/{campaign_id}/run")
def run_campaign_now(campaign_id: int, db: Session = Depends(get_db)):
    campaign = db.get(Campaign, campaign_id)
    if not campaign:
        raise HTTPException(404, "campaign not found")
    with _running_lock:
        if campaign_id in _running:
            raise HTTPException(409, "this campaign is already running")
        _running.add(campaign_id)
    threading.Thread(
        target=_run_campaign_bg, args=(campaign_id, "manual"), daemon=True
    ).start()
    return {"started": True, "campaign_id": campaign_id}


@router.get("/campaigns/{campaign_id}/runs")
def campaign_runs(campaign_id: int, db: Session = Depends(get_db)):
    runs = db.execute(
        select(CampaignRun).where(CampaignRun.campaign_id == campaign_id)
        .order_by(CampaignRun.started_at.desc()).limit(20)
    ).scalars().all()
    return [{
        "id": r.id, "status": r.status, "trigger": r.trigger,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "stats": r.stats, "error": r.error,
    } for r in runs]


@router.get("/workflow")
def workflow_state(db: Session = Depends(get_db)):
    """Live state of the agent graph for the main dashboard.

    Returns the most recent run - active if one is in flight, otherwise the
    last finished one, so the canvas always has something real to show.
    """
    from app.services.pipeline import PIPELINE_STAGES

    run = db.execute(
        select(CampaignRun).where(CampaignRun.status == "running")
        .order_by(CampaignRun.started_at.desc()).limit(1)
    ).scalar_one_or_none()
    if run is None:
        run = db.execute(
            select(CampaignRun).order_by(CampaignRun.started_at.desc()).limit(1)
        ).scalar_one_or_none()

    idle_nodes = [
        {"key": k, "label": lab, "note": note, "icon": ic, "engine": eng,
         "status": "idle", "passed": 0, "dropped": 0, "detail": "",
         "drop_detail": "", "ms": 0}
        for k, lab, note, ic, eng in PIPELINE_STAGES
    ]
    if run is None:
        return {
            "run": None, "nodes": idle_nodes, "log": [],
            "campaigns_ready": db.execute(select(func.count(Campaign.id))).scalar_one(),
        }

    campaign = db.get(Campaign, run.campaign_id)
    state = run.stage_state or {}
    nodes = state.get("nodes") or idle_nodes
    return {
        "active": state.get("active"),
        "run": {
            "id": run.id, "status": run.status, "trigger": run.trigger,
            "campaign": campaign.name if campaign else "—",
            "campaign_id": run.campaign_id,
            "service": campaign.service_category if campaign else "",
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "current_item": run.current_item,
            "stats": run.stats or {},
            "error": run.error,
        },
        "nodes": nodes,
        "log": (run.log or [])[-60:],
    }


@router.get("/runs/{run_id}")
def run_detail(run_id: int, db: Session = Depends(get_db)):
    r = db.get(CampaignRun, run_id)
    if not r:
        raise HTTPException(404, "run not found")
    queries = db.execute(
        select(SearchQuery).where(SearchQuery.run_id == run_id)
    ).scalars().all()
    return {
        "id": r.id, "campaign_id": r.campaign_id, "status": r.status,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "stats": r.stats, "log": r.log, "error": r.error,
        "nodes": (r.stage_state or {}).get("nodes") or [],
        "current_item": r.current_item,
        "queries": [{"source": q.source, "query": q.query, "results": q.results_count} for q in queries],
    }


@router.get("/runs")
def recent_runs(db: Session = Depends(get_db)):
    rows = db.execute(
        select(CampaignRun, Campaign.name)
        .join(Campaign, Campaign.id == CampaignRun.campaign_id)
        .order_by(CampaignRun.started_at.desc()).limit(15)
    ).all()
    return [{
        "id": r.id, "campaign": name, "status": r.status, "trigger": r.trigger,
        "started_at": r.started_at.isoformat() if r.started_at else None,
        "stats": r.stats,
    } for r, name in rows]


# ---------------------------------------------------------------------------
# Leads
# ---------------------------------------------------------------------------


def _lead_row(lead: Lead, company: Company, contact: Contact | None) -> dict:
    return {
        "id": lead.id, "company_id": company.id, "company": company.name,
        "country": company.country, "city": company.city,
        "website": company.website, "industry": company.industry,
        "phone": company.phone,
        "contact_name": contact.full_name if contact else None,
        "email": contact.email if contact else None,
        "service_category": lead.service_category, "intent_level": lead.intent_level,
        "score": lead.score, "stage": lead.stage, "status": lead.status,
        "source_kind": lead.source_kind, "intent_reason": lead.intent_reason,
        "email_policy": company.email_policy,
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "last_contact_at": lead.last_contact_at.isoformat() if lead.last_contact_at else None,
    }


@router.get("/leads")
def list_leads(
    service: str = "", source: str = "", country: str = "", intent: str = "",
    status: str = "", stage: str = "", min_score: int = 0, q: str = "",
    date_from: str = "", date_to: str = "", has_email: str = "",
    sort: str = "score", page: int = 1, page_size: int = 50,
    db: Session = Depends(get_db),
):
    stmt = select(Lead, Company, Contact).join(Company, Company.id == Lead.company_id).outerjoin(
        Contact, Contact.id == Lead.contact_id
    )
    if service:
        stmt = stmt.where(Lead.service_category == service)
    if source:
        stmt = stmt.where(Lead.source_kind == source)
    if country:
        stmt = stmt.where(Company.country == country.upper())
    if intent:
        stmt = stmt.where(Lead.intent_level == intent)
    if status:
        stmt = stmt.where(Lead.status == status)
    if stage:
        stmt = stmt.where(Lead.stage == stage)
    if min_score:
        stmt = stmt.where(Lead.score >= min_score)
    if has_email == "yes":
        stmt = stmt.where(Lead.contact_id.isnot(None))
    elif has_email == "no":
        stmt = stmt.where(Lead.contact_id.is_(None))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(func.lower(Company.name).like(like))
    if date_from:
        stmt = stmt.where(func.date(Lead.created_at) >= date_from)
    if date_to:
        stmt = stmt.where(func.date(Lead.created_at) <= date_to)

    total = db.execute(
        select(func.count()).select_from(stmt.subquery())
    ).scalar_one()

    order = {
        "score": Lead.score.desc(), "created": Lead.created_at.desc(),
        "company": Company.name.asc(), "oldest": Lead.created_at.asc(),
    }.get(sort, Lead.score.desc())
    stmt = stmt.order_by(order, Lead.id.desc()).limit(page_size).offset((page - 1) * page_size)

    rows = db.execute(stmt).all()
    return {
        "total": total, "page": page, "page_size": page_size,
        "leads": [_lead_row(l, c, ct) for l, c, ct in rows],
    }


@router.get("/leads/{lead_id}")
def lead_detail(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    company = db.get(Company, lead.company_id)
    contact = db.get(Contact, lead.contact_id) if lead.contact_id else None

    signals = company_signals(db, company.id)
    sources = {s.id: s for s in db.execute(select(Source)).scalars()} if signals else {}
    audit = db.execute(
        select(WebsiteAudit).where(WebsiteAudit.company_id == company.id)
        .order_by(WebsiteAudit.audited_at.desc()).limit(1)
    ).scalar_one_or_none()
    analyses = db.execute(
        select(AIAnalysis).where(AIAnalysis.lead_id == lead_id).order_by(AIAnalysis.created_at)
    ).scalars().all()
    messages = db.execute(
        select(EmailMessage).where(EmailMessage.lead_id == lead_id)
        .order_by(EmailMessage.created_at.desc())
    ).scalars().all()
    contacts = db.execute(
        select(Contact).where(Contact.company_id == company.id)
    ).scalars().all()

    policy, policy_reason = policy_for_country(company.country)
    return {
        **_lead_row(lead, company, contact),
        "address": company.address,
        "notes": lead.notes,
        "score_breakdown": lead.score_breakdown,
        "policy_reason": policy_reason,
        "contacts": [
            {"id": c.id, "email": c.email, "name": c.full_name, "title": c.title,
             "status": c.email_status} for c in contacts
        ],
        "signals": [{
            "id": s.id, "type": s.signal_type, "evidence_class": s.evidence_class,
            "quote": s.quote, "detected_by": s.detected_by,
            "observed_at": s.observed_at.isoformat() if s.observed_at else None,
            "source_url": sources[s.source_id].url if s.source_id in sources else "",
            "source_kind": sources[s.source_id].kind if s.source_id in sources else "",
        } for s in signals],
        "audit": None if not audit else {
            "url": audit.url, "has_site": audit.has_site, "has_ssl": audit.has_ssl,
            "is_mobile_friendly": audit.is_mobile_friendly, "has_booking": audit.has_booking,
            "has_ecommerce": audit.has_ecommerce, "has_contact_form": audit.has_contact_form,
            "copyright_year": audit.copyright_year, "tech": audit.tech,
            "ttfb_ms": audit.ttfb_ms, "notes": audit.notes,
            "audited_at": audit.audited_at.isoformat() if audit.audited_at else None,
        },
        "ai_analyses": [{
            "agent": a.agent, "model": a.model, "output": a.output,
            "cost_usd": a.cost_usd, "cited": a.output.get("cited_signal_ids", []),
            "created_at": a.created_at.isoformat() if a.created_at else None,
        } for a in analyses],
        "messages": [{
            "id": m.id, "subject": m.subject, "body": m.body, "status": m.status,
            "to_email": m.to_email, "cited_signal_ids": m.cited_signal_ids,
            "failure_reason": m.failure_reason,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "sent_at": m.sent_at.isoformat() if m.sent_at else None,
        } for m in messages],
    }


class LeadPatch(BaseModel):
    status: str | None = None
    notes: str | None = None


@router.patch("/leads/{lead_id}")
def patch_lead(lead_id: int, payload: LeadPatch, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    if payload.status is not None:
        lead.status = payload.status
    if payload.notes is not None:
        lead.notes = payload.notes
    db.commit()
    return {"ok": True}


@router.post("/leads/{lead_id}/rescore")
def rescore_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    company = db.get(Company, lead.company_id)
    contact = db.get(Contact, lead.contact_id) if lead.contact_id else None
    signals = company_signals(db, company.id)
    result = scoring.score_lead(
        company=company, signals=signals, contact=contact,
        service_category=lead.service_category,
    )
    lead.score, lead.intent_level = result.score, result.intent_level
    lead.score_breakdown, lead.score_version = result.breakdown, scoring.SCORE_VERSION
    db.commit()
    return {"score": result.score, "intent_level": result.intent_level,
            "breakdown": result.breakdown}


@router.post("/leads/{lead_id}/draft")
def draft_lead(lead_id: int, db: Session = Depends(get_db)):
    lead = db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(404, "lead not found")
    company = db.get(Company, lead.company_id)
    contact = db.get(Contact, lead.contact_id) if lead.contact_id else None
    if not contact:
        raise HTTPException(400, "this lead has no email address")
    signals = company_signals(db, company.id)
    if not signals:
        raise HTTPException(400, "no evidence on file — refusing to write outreach")

    draft = ai.draft_email(company, signals, contact, lead.service_category, mailer.sender_profile())
    usage = draft.pop("_usage", {})
    db.add(AIAnalysis(
        lead_id=lead.id, agent="personalize", model=usage.get("model", ""),
        input_signal_ids=[s.id for s in signals],
        output={k: v for k, v in draft.items() if not k.startswith("_")},
        tokens_in=usage.get("tokens_in", 0), tokens_out=usage.get("tokens_out", 0),
        cost_usd=usage.get("cost_usd", 0.0),
    ))
    message = EmailMessage(
        lead_id=lead.id, contact_id=contact.id, to_email=contact.email,
        subject=draft["subject"], body=draft["body"],
        cited_signal_ids=draft.get("cited_signal_ids", []),
        unsubscribe_token=mailer.new_unsubscribe_token(),
        status="pending_approval", scheduled_for=utcnow(),
    )
    db.add(message)
    lead.stage = "pending_approval"
    db.commit()
    return {"message_id": message.id, "subject": message.subject, "body": message.body,
            "used_fallback": draft.get("_fallback", False)}


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------


@router.get("/outbox")
def outbox(status: str = "pending_approval", db: Session = Depends(get_db)):
    rows = db.execute(
        select(EmailMessage, Lead, Company)
        .join(Lead, Lead.id == EmailMessage.lead_id)
        .join(Company, Company.id == Lead.company_id)
        .where(EmailMessage.status == status)
        .order_by(Lead.score.desc(), EmailMessage.created_at.asc())
        .limit(200)
    ).all()
    out = []
    for m, lead, company in rows:
        signals = company_signals(db, company.id)
        cited = set(m.cited_signal_ids or [])
        out.append({
            "id": m.id, "lead_id": lead.id, "company": company.name,
            "country": company.country, "website": company.website,
            "to_email": m.to_email, "subject": m.subject, "body": m.body,
            "score": lead.score, "intent_level": lead.intent_level,
            "service_category": lead.service_category, "status": m.status,
            "failure_reason": m.failure_reason,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "evidence": [{
                "id": s.id, "type": s.signal_type, "quote": s.quote,
                "evidence_class": s.evidence_class, "cited": s.id in cited,
            } for s in signals],
        })
    return out


class MessagePatch(BaseModel):
    subject: str | None = None
    body: str | None = None


@router.patch("/outbox/{message_id}")
def edit_message(message_id: int, payload: MessagePatch, db: Session = Depends(get_db)):
    m = db.get(EmailMessage, message_id)
    if not m:
        raise HTTPException(404, "message not found")
    if m.status not in ("pending_approval", "draft", "queued"):
        raise HTTPException(400, f"cannot edit a message that is '{m.status}'")
    if payload.subject is not None:
        m.subject = payload.subject
    if payload.body is not None:
        m.body = payload.body
    db.commit()
    return {"ok": True}


@router.post("/outbox/{message_id}/approve")
def approve_message(message_id: int, send_now: bool = True, db: Session = Depends(get_db)):
    m = db.get(EmailMessage, message_id)
    if not m:
        raise HTTPException(404, "message not found")
    lead = db.get(Lead, m.lead_id)
    company = db.get(Company, lead.company_id)
    contact = db.get(Contact, m.contact_id) if m.contact_id else None

    gate = mailer.check_can_send(db, lead, company, contact, exclude_message_id=m.id)
    if not gate.ok:
        raise HTTPException(400, f"cannot send: {gate.reason}")

    m.status = "queued"
    m.approved_at = utcnow()
    m.approved_by = "admin"
    lead.stage = "queued"
    db.commit()

    if send_now:
        sent, detail = mailer.send_message(db, m)
        db.commit()
        return {"ok": sent, "detail": detail, "status": m.status}
    return {"ok": True, "detail": "queued", "status": m.status}


@router.post("/outbox/{message_id}/reject")
def reject_message(message_id: int, db: Session = Depends(get_db)):
    m = db.get(EmailMessage, message_id)
    if not m:
        raise HTTPException(404, "message not found")
    m.status = "rejected"
    lead = db.get(Lead, m.lead_id)
    if lead:
        lead.stage = "rejected"
        lead.status = "rejected_by_admin"
    db.commit()
    return {"ok": True}


@router.post("/outbox/dispatch")
def dispatch_now(limit: int = 25, db: Session = Depends(get_db)):
    return pipeline.dispatch_queue(db, limit=limit)


# ---------------------------------------------------------------------------
# Suppressions & unsubscribe
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WhatsApp, alerts, inbox
# ---------------------------------------------------------------------------


@router.get("/linkedin")
def linkedin_queue(only_unemailable: bool = False, db: Session = Depends(get_db)):
    return {
        "stats": linkedin.stats(db),
        "leads": linkedin.queue(db, only_unemailable=only_unemailable),
    }


@router.get("/whatsapp")
def whatsapp_queue(status: str = "pending", db: Session = Depends(get_db)):
    rows = db.execute(
        select(WhatsAppMessage, Lead, Company)
        .join(Lead, Lead.id == WhatsAppMessage.lead_id)
        .join(Company, Company.id == Lead.company_id)
        .where(WhatsAppMessage.status == status)
        .order_by(Lead.score.desc(), WhatsAppMessage.created_at.asc())
        .limit(200)
    ).all()
    return [{
        "id": m.id, "lead_id": lead.id, "company": company.name,
        "country": company.country, "phone": m.phone_e164, "body": m.body,
        "score": lead.score, "status": m.status,
        "service_category": lead.service_category,
        "link": whatsapp.wa_link(m.phone_e164, m.body),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    } for m, lead, company in rows]


@router.post("/whatsapp/{message_id}/{action}")
def whatsapp_mark(message_id: int, action: str, db: Session = Depends(get_db)):
    if action not in ("opened", "sent", "skipped"):
        raise HTTPException(400, "action must be opened, sent or skipped")
    if not whatsapp.mark(db, message_id, action):
        raise HTTPException(404, "message not found")
    db.commit()
    return {"ok": True}


@router.post("/notify/test")
def notify_test():
    if not notify.ready():
        raise HTTPException(400, "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set")
    ok, detail = notify.send(
        "<b>✅ Money Maker connected</b>\n\nReply alerts will arrive here."
    )
    if not ok:
        raise HTTPException(502, f"Telegram rejected it: {detail}")
    return {"ok": True}


@router.post("/inbox/poll")
def inbox_poll(db: Session = Depends(get_db)):
    if not inbox.ready():
        raise HTTPException(400, "IMAP is not configured")
    return inbox.poll(db)


@router.get("/suppressions")
def list_suppressions(db: Session = Depends(get_db)):
    rows = db.execute(select(Suppression).order_by(Suppression.created_at.desc())).scalars().all()
    return [{"id": s.id, "scope": s.scope, "value": s.value, "reason": s.reason,
             "created_at": s.created_at.isoformat() if s.created_at else None} for s in rows]


@router.post("/suppressions")
def add_suppression(
    value: str = Body(..., embed=True), scope: str = Body("email", embed=True),
    reason: str = Body("added manually", embed=True), db: Session = Depends(get_db),
):
    suppress(db, value, reason, scope)
    db.commit()
    return {"ok": True}


@router.delete("/suppressions/{suppression_id}")
def remove_suppression(suppression_id: int, db: Session = Depends(get_db)):
    db.execute(delete(Suppression).where(Suppression.id == suppression_id))
    db.commit()
    return {"ok": True}


@router.get("/mailboxes")
def list_mailboxes(db: Session = Depends(get_db)):
    rows = db.execute(select(Mailbox)).scalars().all()
    return [{
        "id": m.id, "address": m.address, "enabled": m.enabled,
        "daily_cap": m.daily_cap, "effective_cap_today": mailer.warmup_cap(m),
        "sent_today": mailer.sent_today(db, m.id),
        "warmup_started_at": m.warmup_started_at.isoformat() if m.warmup_started_at else None,
    } for m in rows]


@router.get("/settings")
def read_settings():
    """Non-secret configuration, plus whether each secret is present."""
    return {
        "company_name": settings.company_name,
        "company_website": settings.company_website,
        "company_postal_address": settings.company_postal_address,
        "sender_name": settings.sender_name,
        "sender_email": settings.sender_email,
        "portfolio_url": settings.portfolio_url,
        "privacy_policy_url": settings.privacy_policy_url,
        "public_base_url": settings.public_base_url,
        "daily_email_cap_global": settings.daily_email_cap_global,
        "per_mailbox_daily_cap": settings.per_mailbox_daily_cap,
        "cooling_off_days": settings.cooling_off_days,
        "min_score_to_email": settings.min_score_to_email,
        "configured": {
            "anthropic_api_key": bool(settings.anthropic_api_key),
            "google_places_key": bool(settings.google_places_key),
            "companies_house_key": bool(settings.companies_house_key),
            "adzuna": bool(settings.adzuna_app_id and settings.adzuna_app_key),
            "smtp": bool(settings.smtp_host),
        },
    }


@router.delete("/leads/purge-evidenceless")
def purge_evidenceless(db: Session = Depends(get_db)):
    """Delete every lead whose company has no evidence on file.

    A lead with no signal is a name, not a prospect: nothing can be said to
    them truthfully, so it can never be emailed. Mostly this clears rows
    imported from the previous version.
    """
    company_ids = set(db.execute(select(Signal.company_id)).scalars().all())
    leads = db.execute(select(Lead, Company).join(Company, Company.id == Lead.company_id)).all()
    removed, kept = 0, 0
    for lead, company in leads:
        if company.id in company_ids:
            kept += 1
            continue
        db.delete(lead)
        db.delete(company)
        removed += 1
    db.commit()
    return {"removed": removed, "kept": kept}


@router.post("/legacy/import")
def legacy_import(db: Session = Depends(get_db)):
    """Opt-in import of the previous version's leads, as history only."""
    from app.seed import migrate_legacy

    return {"imported": migrate_legacy(db)}


@router.get("/sources")
def source_guide(db: Session = Depends(get_db)):
    """What each source is for, whether it is ready, and which key to get next."""
    from app.seed import DEFAULT_CAMPAIGNS, FREE_KEY_PRIORITY

    by_service = {
        c["service_category"]: c["sources"] for c in DEFAULT_CAMPAIGNS
    }
    return {
        "sources": available_sources(),
        "recommended": by_service,
        "free_keys": [
            {**k, "configured": _key_configured(k["name"])} for k in FREE_KEY_PRIORITY
        ],
    }


def _key_configured(name: str) -> bool:
    return {
        "Serper": bool(settings.serper_api_key),
        "Adzuna": bool(settings.adzuna_app_id and settings.adzuna_app_key),
        "Companies House": bool(settings.companies_house_key),
        "SAM.gov": bool(getattr(settings, "sam_gov_api_key", "")),
        "Yelp Fusion": bool(getattr(settings, "yelp_api_key", "")),
    }.get(name, False)


@router.post("/sources/apply-recommended")
def apply_recommended(db: Session = Depends(get_db)):
    from app.seed import apply_recommended_sources
    from app.scheduler import reschedule

    changed = apply_recommended_sources(db)
    reschedule()
    return {"ok": True, "campaigns_updated": changed}


@router.get("/export/leads.xlsx")
def export_leads(db: Session = Depends(get_db)):
    from io import BytesIO

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    rows = db.execute(
        select(Lead, Company, Contact)
        .join(Company, Company.id == Lead.company_id)
        .outerjoin(Contact, Contact.id == Lead.contact_id)
        .order_by(Lead.score.desc())
    ).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"
    headers = ["Score", "Intent", "Company", "Country", "City", "Industry", "Website",
               "Contact", "Email", "Phone", "Service", "Source", "Stage", "Status",
               "Why this lead", "Created"]
    fill, font = PatternFill("solid", fgColor="0E7490"), Font(color="FFFFFF", bold=True)
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=1, column=i, value=h)
        c.fill, c.font, c.alignment = fill, font, Alignment(horizontal="left")
    for r, (lead, company, contact) in enumerate(rows, 2):
        for i, v in enumerate([
            lead.score, lead.intent_level, company.name, company.country, company.city,
            company.industry, company.website, contact.full_name if contact else "",
            contact.email if contact else "", company.phone, lead.service_category,
            lead.source_kind, lead.stage, lead.status, lead.intent_reason,
            lead.created_at.strftime("%Y-%m-%d %H:%M") if lead.created_at else "",
        ], 1):
            ws.cell(row=r, column=i, value=v)
    for i, w in enumerate([7, 9, 30, 8, 16, 22, 34, 20, 30, 18, 18, 16, 14, 16, 60, 18], 1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = w
    ws.freeze_panes = "A2"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"moneymaker_leads_{dt.date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- public, unauthenticated ----------------------------------------------

public_router = APIRouter()


@public_router.api_route("/u/{token}", methods=["GET", "POST"], response_class=HTMLResponse)
def unsubscribe(token: str, db: Session = Depends(get_db)):
    """One-click unsubscribe. POST is what RFC 8058 clients call."""
    message = db.execute(
        select(EmailMessage).where(EmailMessage.unsubscribe_token == token)
    ).scalar_one_or_none()
    if message:
        suppress(db, message.to_email, "unsubscribed via email link")
        db.add(EmailEvent(message_id=message.id, type="unsubscribe", payload={}))
        lead = db.get(Lead, message.lead_id)
        if lead:
            lead.status = "unsubscribed"
            lead.stage = "closed"
        db.commit()
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<title>Unsubscribed</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:15vh auto;"
        "padding:0 1.5rem;color:#16202E;line-height:1.6}</style>"
        "<h1>You've been unsubscribed</h1>"
        "<p>You won't receive any further emails from us. "
        "Sorry for the interruption.</p>"
    )
