"""Admin panel API: customers, bots and prices, payments, platform numbers.

Every route here requires the admin role. Customers never see each other's
data; the admin sees accounts, subscriptions and payments, plus usage counts,
but customers' leads and emails stay in their own workspaces.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api import Me, _unlock, admin_only
from app.config import settings
from app.db import get_db
from app.models import (
    Bot, Campaign, CampaignRun, EmailMessage, Lead, Payment, Subscription, User, as_aware, utcnow,
)
from app.services import auth, billing, platform_mail, tenant, workspace

router = APIRouter(prefix="/api/admin", dependencies=[Depends(admin_only)])


def _iso(v):
    return as_aware(v).isoformat() if v else None


def _money(rows) -> dict:
    """{currency: whole units} from (currency, smallest-unit sum) rows."""
    return {cur: round((amount or 0) / 100, 2) for cur, amount in rows if cur}


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    now = utcnow()
    d30, d7 = now - dt.timedelta(days=30), now - dt.timedelta(days=7)
    customers = select(func.count(User.id)).where(User.role == "customer")
    live = (Subscription.revoked_at.is_(None)) & (Subscription.expires_at > now)

    paid_live = db.execute(select(Subscription).where(live, Subscription.kind == "paid")).scalars().all()
    mrr: dict[str, float] = {}
    for s in paid_live:
        monthly = s.amount / 100 / (12 if s.period == "year" else 1)
        mrr[s.currency] = round(mrr.get(s.currency, 0) + monthly, 2)

    signups = dict(db.execute(
        select(func.date(User.created_at), func.count(User.id))
        .where(User.role == "customer", User.created_at >= d30)
        .group_by(func.date(User.created_at))).all())
    revenue_days = db.execute(
        select(func.date(Payment.paid_at), Payment.currency, func.sum(Payment.amount))
        .where(Payment.status == "paid", Payment.paid_at >= d30)
        .group_by(func.date(Payment.paid_at), Payment.currency)).all()
    rev_map: dict[str, dict] = {}
    for day, cur, amount in revenue_days:
        rev_map.setdefault(str(day), {})[cur] = round((amount or 0) / 100, 2)
    series = []
    for i in range(29, -1, -1):
        day = (now - dt.timedelta(days=i)).date().isoformat()
        series.append({"day": day, "signups": signups.get(day, 0) or signups.get(dt.date.fromisoformat(day), 0),
                       "revenue": rev_map.get(day, {})})

    by_bot = dict(db.execute(
        select(Subscription.bot_key, func.count(func.distinct(Subscription.user_id)))
        .where(live, Subscription.kind.in_(("paid", "grant"))).group_by(Subscription.bot_key)).all())
    trials_by_bot = dict(db.execute(
        select(Subscription.bot_key, func.count(Subscription.id))
        .where(live, Subscription.kind == "trial").group_by(Subscription.bot_key)).all())
    names = {b.key: b.name for b in billing.catalog(db, include_inactive=True)}

    recent_pay = db.execute(
        select(Payment, User.email, User.name).join(User, User.id == Payment.user_id)
        .order_by(Payment.created_at.desc()).limit(8)).all()
    recent_users = db.execute(
        select(User).where(User.role == "customer").order_by(User.created_at.desc()).limit(8)).scalars().all()

    return {
        "customers": db.execute(customers).scalar_one(),
        "new_7d": db.execute(customers.where(User.created_at >= d7)).scalar_one(),
        "new_30d": db.execute(customers.where(User.created_at >= d30)).scalar_one(),
        "suspended": db.execute(customers.where(User.status == "suspended")).scalar_one(),
        "paying_customers": db.execute(select(func.count(func.distinct(Subscription.user_id)))
                                       .where(live, Subscription.kind == "paid")).scalar_one(),
        "active_subscriptions": len(paid_live),
        "live_trials": db.execute(select(func.count(Subscription.id))
                                  .where(live, Subscription.kind == "trial")).scalar_one(),
        "trials_started": db.execute(select(func.count(User.id))
                                     .where(User.trial_started_at.isnot(None))).scalar_one(),
        "mrr": mrr,
        "revenue_total": _money(db.execute(select(Payment.currency, func.sum(Payment.amount))
                                           .where(Payment.status == "paid").group_by(Payment.currency)).all()),
        "revenue_30d": _money(db.execute(select(Payment.currency, func.sum(Payment.amount))
                                         .where(Payment.status == "paid", Payment.paid_at >= d30)
                                         .group_by(Payment.currency)).all()),
        "series": series,
        "bots": [{"key": k, "name": names.get(k, k), "subscribers": by_bot.get(k, 0),
                  "trials": trials_by_bot.get(k, 0)} for k in names],
        "recent_payments": [{
            "id": p.id, "email": email, "name": name, "bot": names.get(p.bot_key, p.bot_key),
            "period": p.period, "currency": p.currency, "amount": p.amount, "status": p.status,
            "created_at": _iso(p.created_at),
        } for p, email, name in recent_pay],
        "recent_signups": [{"id": u.id, "name": u.name, "email": u.email, "company": u.company,
                            "created_at": _iso(u.created_at)} for u in recent_users],
        "razorpay_ready": billing.razorpay_ready(),
        "webhook_ready": bool(settings.razorpay_webhook_secret),
        "account_mail_ready": platform_mail.ready(),
        # Agents, leads and emails still owned by the admin account, from
        # before the app had customers. The panel offers to move them.
        "admin_workspace": workspace.counts(db, tenant.admin_id(db)),
    }


@router.get("/customers")
def customers(q: str = "", status: str = "", db: Session = Depends(get_db)):
    stmt = select(User).where(User.role == "customer")
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(like), func.lower(User.name).like(like),
                              func.lower(User.company).like(like)))
    if status:
        stmt = stmt.where(User.status == status)
    users = db.execute(stmt.order_by(User.created_at.desc()).limit(500)).scalars().all()
    ids = [u.id for u in users]
    leads = dict(db.execute(select(Lead.owner_id, func.count(Lead.id))
                            .where(Lead.owner_id.in_(ids)).group_by(Lead.owner_id)).all()) if ids else {}
    sent = dict(db.execute(select(EmailMessage.owner_id, func.count(EmailMessage.id))
                           .where(EmailMessage.owner_id.in_(ids), EmailMessage.status == "sent")
                           .group_by(EmailMessage.owner_id)).all()) if ids else {}
    paid = {}
    for uid, cur, amount in db.execute(
            select(Payment.user_id, Payment.currency, func.sum(Payment.amount))
            .where(Payment.user_id.in_(ids), Payment.status == "paid")
            .group_by(Payment.user_id, Payment.currency)).all() if ids else []:
        paid.setdefault(uid, {})[cur] = round((amount or 0) / 100, 2)
    out = []
    for u in users:
        access = billing.access_map(db, u.id)
        out.append({
            "id": u.id, "name": u.name, "email": u.email, "company": u.company,
            "status": u.status, "created_at": _iso(u.created_at), "last_login_at": _iso(u.last_login_at),
            "access": access, "trial_bot": u.trial_bot, "trial_started_at": _iso(u.trial_started_at),
            "leads": leads.get(u.id, 0), "emails_sent": sent.get(u.id, 0), "paid": paid.get(u.id, {}),
        })
    return out


def _customer(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None or user.role != "customer":
        raise HTTPException(404, "customer not found")
    return user


@router.get("/customers/{user_id}")
def customer_detail(user_id: int, db: Session = Depends(get_db)):
    u = _customer(db, user_id)
    subs = db.execute(select(Subscription).where(Subscription.user_id == u.id)
                      .order_by(Subscription.created_at.desc())).scalars().all()
    pays = db.execute(select(Payment).where(Payment.user_id == u.id)
                      .order_by(Payment.created_at.desc())).scalars().all()
    camps = db.execute(select(Campaign).where(Campaign.owner_id == u.id)).scalars().all()
    runs = db.execute(select(func.count(CampaignRun.id)).where(CampaignRun.owner_id == u.id)).scalar_one()
    return {
        "id": u.id, "name": u.name, "email": u.email, "company": u.company, "status": u.status,
        "email_verified": u.email_verified_at is not None, "terms_accepted_at": _iso(u.terms_accepted_at),
        "created_at": _iso(u.created_at), "last_login_at": _iso(u.last_login_at),
        "trial_bot": u.trial_bot, "trial_started_at": _iso(u.trial_started_at),
        "access": billing.access_map(db, u.id),
        "usage": {
            "campaigns": len(camps), "runs": runs,
            "leads": db.execute(select(func.count(Lead.id)).where(Lead.owner_id == u.id)).scalar_one(),
            "emails_sent": db.execute(select(func.count(EmailMessage.id)).where(
                EmailMessage.owner_id == u.id, EmailMessage.status == "sent")).scalar_one(),
        },
        "campaigns": [{"id": c.id, "name": c.name, "bot": c.service_category, "enabled": c.enabled}
                      for c in camps],
        "subscriptions": [{
            "id": s.id, "bot": s.bot_key, "kind": s.kind, "period": s.period,
            "currency": s.currency, "amount": s.amount, "starts_at": _iso(s.starts_at),
            "expires_at": _iso(s.expires_at), "revoked": s.revoked_at is not None, "note": s.note,
        } for s in subs],
        "payments": [{
            "id": p.id, "bot": p.bot_key, "period": p.period, "currency": p.currency,
            "amount": p.amount, "status": p.status, "order_id": p.order_id,
            "payment_id": p.provider_payment_id, "error": p.error,
            "created_at": _iso(p.created_at), "paid_at": _iso(p.paid_at),
        } for p in pays],
    }


class CustomerPatch(BaseModel):
    # "suspended" is what the panel calls Blocked: the account and all its data
    # are kept, but nobody can sign in and its agents and sending are paused.
    status: str | None = None
    email_verified: bool | None = None  # lets you confirm an address by hand
    name: str | None = Field(None, max_length=160)
    company: str | None = Field(None, max_length=160)


@router.patch("/customers/{user_id}")
def update_customer(user_id: int, payload: CustomerPatch, db: Session = Depends(get_db)):
    u = _customer(db, user_id)
    if payload.status is not None:
        if payload.status not in ("active", "suspended"):
            raise HTTPException(400, "status must be active or suspended")
        u.status = payload.status
        if payload.status == "suspended":
            auth.destroy_all_sessions(db, u.id)  # signed out everywhere at once
    if payload.email_verified is not None:
        u.email_verified_at = utcnow() if payload.email_verified else None
    if payload.name is not None:
        u.name = payload.name.strip() or u.name
    if payload.company is not None:
        u.company = payload.company.strip() or None
    db.commit()
    auth._forget_sessions()  # so a block takes effect now, not after the 60 s cache
    return {"ok": True}


class GrantIn(BaseModel):
    bot: str
    days: int = Field(30, ge=1, le=3650)
    note: str = ""


@router.post("/customers/{user_id}/grant")
def grant_access(user_id: int, payload: GrantIn, me: Me = Depends(admin_only),
                 db: Session = Depends(get_db)):
    u = _customer(db, user_id)
    if not db.execute(select(Bot).where(Bot.key == payload.bot)).scalar_one_or_none():
        raise HTTPException(404, "bot not found")
    sub = billing.grant(db, u.id, payload.bot, payload.days, payload.note)
    db.commit()
    _unlock(db, u.id, payload.bot)
    return {"ok": True, "expires_at": _iso(sub.expires_at)}


class RevokeIn(BaseModel):
    bot: str


@router.delete("/customers/{user_id}")
def delete_customer(user_id: int, db: Session = Depends(get_db)):
    """Delete the account and everything it owns. This cannot be undone."""
    from app.scheduler import reschedule

    u = _customer(db, user_id)
    gone = workspace.delete_account(db, u)
    reschedule()
    return {"ok": True, "deleted": gone}


@router.post("/customers/{user_id}/revoke")
def revoke_access(user_id: int, payload: RevokeIn, db: Session = Depends(get_db)):
    u = _customer(db, user_id)
    n = billing.revoke(db, u.id, payload.bot)
    db.commit()
    return {"ok": True, "revoked": n}


class ResetIn(BaseModel):
    password: str = Field(min_length=8, max_length=200)


@router.post("/customers/{user_id}/password")
def reset_password(user_id: int, payload: ResetIn, db: Session = Depends(get_db)):
    u = _customer(db, user_id)
    u.password_hash = auth.hash_password(payload.password)
    auth.destroy_all_sessions(db, u.id)
    db.commit()
    return {"ok": True}


class TransferIn(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field("", max_length=200)
    name: str = Field("", max_length=160)


@router.post("/transfer-workspace")
def transfer_workspace(payload: TransferIn, db: Session = Depends(get_db)):
    """Hand the admin's own agents, leads and emails to a customer account."""
    if "@" not in payload.email:
        raise HTTPException(422, "Enter a valid email address.")
    try:
        return {"ok": True, **workspace.transfer_admin_workspace(
            db, email=payload.email, password=payload.password, name=payload.name)}
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))


# --- bots & prices -------------------------------------------------------------------

@router.get("/bots")
def list_bots(db: Session = Depends(get_db)):
    now = utcnow()
    live = (Subscription.revoked_at.is_(None)) & (Subscription.expires_at > now)
    subs = dict(db.execute(select(Subscription.bot_key, func.count(func.distinct(Subscription.user_id)))
                           .where(live, Subscription.kind == "paid").group_by(Subscription.bot_key)).all())
    revenue: dict[str, dict] = {}
    for key, cur, amount in db.execute(select(Payment.bot_key, Payment.currency, func.sum(Payment.amount))
                                       .where(Payment.status == "paid")
                                       .group_by(Payment.bot_key, Payment.currency)).all():
        revenue.setdefault(key, {})[cur] = round((amount or 0) / 100, 2)
    return [{**billing.bot_json(b), "subscribers": subs.get(b.key, 0), "revenue": revenue.get(b.key, {})}
            for b in billing.catalog(db, include_inactive=True)]


class BotIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    tagline: str = Field("", max_length=200)
    description: str = Field("", max_length=2000)
    features: list[str] = []
    active: bool = True
    sort_order: int = Field(100, ge=0, le=10000)
    price_inr_month: int = Field(ge=0, le=10_000_000)
    price_inr_year: int = Field(ge=0, le=100_000_000)
    price_usd_month: int = Field(ge=0, le=1_000_000)
    price_usd_year: int = Field(ge=0, le=10_000_000)
    # What the bot's agent does, and how long plans last. Optional so an older
    # page that doesn't send them leaves the current values alone.
    countries: list[str] | None = None
    sources: list[str] | None = None
    schedule_days: str | None = None
    schedule_time: str | None = None
    leads_per_run: int | None = Field(None, ge=1, le=500)
    min_score: int | None = Field(None, ge=0, le=100)
    trial_hours: int | None = Field(None, ge=1, le=24 * 60)
    month_days: int | None = Field(None, ge=1, le=366)
    year_days: int | None = Field(None, ge=1, le=3660)


_DAYS = ("mon-fri", "daily", "weekends")


@router.put("/bots/{key}")
def update_bot(key: str, payload: BotIn, db: Session = Depends(get_db)):
    from app.scheduler import reschedule
    from app.seed import apply_bot_limits
    from app.services.compliance import COUNTRY_POLICY
    from app.services.discovery import available_sources

    bot = db.execute(select(Bot).where(Bot.key == key)).scalar_one_or_none()
    if bot is None:
        raise HTTPException(404, "bot not found")
    data = payload.model_dump(exclude_unset=True)
    if "features" in data:
        data["features"] = [f.strip()[:120] for f in data["features"] if f.strip()][:8]
    if "countries" in data:
        data["countries"] = [c for c in dict.fromkeys(data["countries"] or []) if c in COUNTRY_POLICY]
        if not data["countries"]:
            raise HTTPException(400, "Pick at least one country for this bot.")
    if "sources" in data:
        known = {s["key"] for s in available_sources()}
        data["sources"] = [s for s in dict.fromkeys(data["sources"] or []) if s in known]
    if data.get("schedule_days") not in (None, *_DAYS):
        raise HTTPException(400, "Run days must be Mon-Fri, every day or weekends.")
    if data.get("schedule_time"):
        try:
            h, m = (int(p) for p in data["schedule_time"].split(":")[:2])
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            raise HTTPException(400, "Start time must be HH:MM.")
        data["schedule_time"] = f"{h:02d}:{m:02d}"
    for k, v in data.items():
        if k in ("trial_hours",) or v is not None:  # trial_hours may be cleared back to the default
            setattr(bot, k, v)
    # Every customer's agent for this bot follows the new limits straight away.
    changed = 0
    for c in db.execute(select(Campaign).where(Campaign.service_category == key,
                                               Campaign.owner_id != tenant.admin_id(db))).scalars():
        changed += apply_bot_limits(c, bot)
    db.commit()
    if changed:
        reschedule()
    return {**billing.bot_json(bot), "agents_updated": changed}


# --- payments ----------------------------------------------------------------------

@router.get("/payments")
def payments(status: str = "", db: Session = Depends(get_db)):
    stmt = select(Payment, User.email, User.name).join(User, User.id == Payment.user_id)
    if status:
        stmt = stmt.where(Payment.status == status)
    names = {b.key: b.name for b in billing.catalog(db, include_inactive=True)}
    rows = db.execute(stmt.order_by(Payment.created_at.desc()).limit(500)).all()
    return [{
        "id": p.id, "user_id": p.user_id, "email": email, "name": name,
        "bot": names.get(p.bot_key, p.bot_key), "period": p.period, "currency": p.currency,
        "amount": p.amount, "status": p.status, "order_id": p.order_id,
        "payment_id": p.provider_payment_id, "error": p.error,
        "created_at": _iso(p.created_at), "paid_at": _iso(p.paid_at),
    } for p, email, name in rows]
