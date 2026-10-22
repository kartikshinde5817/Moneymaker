"""Bots, access, and Razorpay payments.

A customer has access to a bot while any Subscription row for it covers the
current moment. Rows come from three places:

  trial  - the one free trial per account (TRIAL_HOURS, one bot)
  paid   - a verified Razorpay payment for a month or a year
  grant  - added by the admin by hand (comped access, refunds, support)

Buying again extends from the current end date rather than from today, so
renewing early never loses days.

Payment is confirmed on the server in two independent ways - the signature
the checkout returns, and Razorpay's webhook - and fulfilment is idempotent,
so whichever arrives first grants access and the other is a no-op. Nothing
the browser claims is trusted on its own.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import logging
import secrets

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Bot, Payment, Subscription, User, as_aware, utcnow

log = logging.getLogger(__name__)

API = "https://api.razorpay.com/v1"
PERIOD_DAYS = {"month": 30, "year": 365}
CURRENCIES = ("INR", "USD")

DEFAULT_BOTS = [
    {
        "key": "web_development", "name": "Web Development Bot", "sort_order": 10,
        "tagline": "Finds businesses with a missing, broken or outdated website.",
        "description": "Audits local businesses' websites and finds the ones that are not "
                       "mobile friendly, insecure, years out of date or missing altogether - "
                       "then writes an email that quotes exactly what it found.",
        "features": ["Website audits: mobile, HTTPS, speed, age", "New companies with no website yet",
                     "Public tenders for website projects", "Evidence-based email drafts"],
    },
    {
        "key": "digital_marketing", "name": "Digital Marketing Bot", "sort_order": 20,
        "tagline": "Finds businesses with weak SEO, no analytics and no social presence.",
        "description": "For marketing agencies. Checks each business's website for missing "
                       "analytics, missing search descriptions, weak page titles and no social "
                       "profiles, and drafts outreach about the gaps it can prove.",
        "features": ["SEO checks: titles and descriptions", "Analytics and tracking detection",
                     "Social media presence check", "People asking for marketing help online"],
    },
    {
        "key": "ai_agent", "name": "AI Agents Bot", "sort_order": 30,
        "tagline": "Finds companies that need chatbots and voice or WhatsApp agents.",
        "description": "Searches for businesses asking for chatbots, AI assistants and "
                       "customer-support automation, and companies hiring for those skills.",
        "features": ["Buying-intent web and LinkedIn search", "Hiring signals from job ads",
                     "Public tenders", "Evidence-based email drafts"],
    },
    {
        "key": "ai_automation", "name": "AI Automation Bot", "sort_order": 40,
        "tagline": "Finds teams looking to automate repetitive work with AI.",
        "description": "Finds businesses asking for workflow and process automation, and "
                       "companies hiring automation roles.",
        "features": ["Buying-intent search", "Hiring signals", "Public tenders",
                     "Evidence-based email drafts"],
    },
    {
        "key": "computer_vision", "name": "Computer Vision Bot", "sort_order": 50,
        "tagline": "Finds factories and warehouses that need camera-based AI.",
        "description": "Finds manufacturers, warehouses and sites looking for PPE detection, "
                       "safety monitoring, quality inspection and CCTV analytics.",
        "features": ["Safety and inspection tenders", "Hiring signals",
                     "Industrial sector search", "Evidence-based email drafts"],
    },
]


# --- catalog ----------------------------------------------------------------------

def seed_bots(db: Session) -> None:
    """Make sure every built-in bot exists. Prices are never overwritten."""
    have = set(db.execute(select(Bot.key)).scalars())
    for spec in DEFAULT_BOTS:
        if spec["key"] not in have:
            db.add(Bot(**spec))
    db.flush()


def ensure_bot_for_service(db: Session, key: str, name: str, tagline: str = "") -> Bot:
    bot = db.execute(select(Bot).where(Bot.key == key)).scalar_one_or_none()
    if bot is None:
        bot = Bot(key=key, name=name if name.lower().endswith("bot") else f"{name} Bot",
                  tagline=tagline[:200], description=tagline, features=[], sort_order=200)
        db.add(bot)
        db.flush()
    return bot


def price(bot: Bot, currency: str, period: str) -> int:
    """Whole rupees / dollars."""
    return int(getattr(bot, f"price_{currency.lower()}_{period}", 0) or 0)


def bot_json(bot: Bot) -> dict:
    return {
        "key": bot.key, "name": bot.name, "tagline": bot.tagline,
        "description": bot.description, "features": bot.features or [],
        "active": bot.active, "sort_order": bot.sort_order,
        "prices": {
            "INR": {"month": bot.price_inr_month, "year": bot.price_inr_year},
            "USD": {"month": bot.price_usd_month, "year": bot.price_usd_year},
        },
    }


def catalog(db: Session, *, include_inactive: bool = False) -> list[Bot]:
    stmt = select(Bot).order_by(Bot.sort_order, Bot.id)
    if not include_inactive:
        stmt = stmt.where(Bot.active.is_(True))
    return list(db.execute(stmt).scalars())


# --- access ------------------------------------------------------------------------

def _live(db: Session, user_id: int):
    now = utcnow()
    return db.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.revoked_at.is_(None),
            Subscription.expires_at > now,
        )
    ).scalars().all()


def access_map(db: Session, user_id: int) -> dict[str, dict]:
    """bot_key -> {"kind", "expires_at"} for every bot the user can use now."""
    from app.services.tenant import is_admin

    if is_admin(db, user_id):
        return {b.key: {"kind": "admin", "expires_at": None} for b in catalog(db, include_inactive=True)}
    out: dict[str, dict] = {}
    for sub in _live(db, user_id):
        cur = out.get(sub.bot_key)
        exp = as_aware(sub.expires_at)
        if cur is None or exp > cur["_exp"]:
            out[sub.bot_key] = {"kind": sub.kind, "expires_at": exp.isoformat(), "_exp": exp}
    for v in out.values():
        v.pop("_exp", None)
    return out


def has_access(db: Session, user_id: int, bot_key: str) -> bool:
    return bot_key in access_map(db, user_id)


def _current_end(db: Session, user_id: int, bot_key: str) -> dt.datetime:
    ends = [as_aware(s.expires_at) for s in _live(db, user_id) if s.bot_key == bot_key and s.kind != "trial"]
    return max([utcnow(), *ends])


class TrialError(Exception):
    pass


def start_trial(db: Session, user: User, bot_key: str) -> Subscription:
    if user.trial_started_at:
        raise TrialError("Your free trial has already been used.")
    bot = db.execute(select(Bot).where(Bot.key == bot_key, Bot.active.is_(True))).scalar_one_or_none()
    if bot is None:
        raise TrialError("That bot is not available.")
    now = utcnow()
    user.trial_bot, user.trial_started_at = bot_key, now
    sub = Subscription(user_id=user.id, bot_key=bot_key, kind="trial", period="day",
                       starts_at=now, expires_at=now + dt.timedelta(hours=settings.trial_hours),
                       note=f"{settings.trial_hours}-hour free trial")
    db.add(sub)
    db.flush()
    return sub


def grant(db: Session, user_id: int, bot_key: str, days: int, note: str = "") -> Subscription:
    start = _current_end(db, user_id, bot_key)
    sub = Subscription(user_id=user_id, bot_key=bot_key, kind="grant", period="custom",
                       starts_at=start, expires_at=start + dt.timedelta(days=days),
                       note=(note or f"{days} days granted by admin")[:255])
    db.add(sub)
    db.flush()
    return sub


def revoke(db: Session, user_id: int, bot_key: str) -> int:
    subs = [s for s in _live(db, user_id) if s.bot_key == bot_key]
    for s in subs:
        s.revoked_at = utcnow()
    db.flush()
    return len(subs)


# --- Razorpay -----------------------------------------------------------------------

class PaymentError(Exception):
    pass


def razorpay_ready() -> bool:
    return bool(settings.razorpay_key_id and settings.razorpay_key_secret)


def create_order(db: Session, user: User, bot_key: str, period: str, currency: str) -> dict:
    if not razorpay_ready():
        raise PaymentError("Online payments are not set up yet. Please contact support.")
    if period not in PERIOD_DAYS or currency not in CURRENCIES:
        raise PaymentError("Choose a monthly or yearly plan in INR or USD.")
    bot = db.execute(select(Bot).where(Bot.key == bot_key, Bot.active.is_(True))).scalar_one_or_none()
    if bot is None:
        raise PaymentError("That bot is not available.")
    whole = price(bot, currency, period)
    if whole <= 0:
        raise PaymentError("This plan has no price set yet.")
    amount = whole * 100
    receipt = f"u{user.id}-{secrets.token_hex(6)}"
    try:
        r = httpx.post(
            f"{API}/orders", auth=(settings.razorpay_key_id, settings.razorpay_key_secret),
            json={"amount": amount, "currency": currency, "receipt": receipt,
                  "notes": {"user_id": str(user.id), "bot": bot_key, "period": period}},
            timeout=20,
        )
    except httpx.HTTPError as exc:
        raise PaymentError(f"Could not reach Razorpay: {exc}") from exc
    if r.status_code != 200:
        detail = (r.json().get("error", {}) or {}).get("description") if r.headers.get(
            "content-type", "").startswith("application/json") else r.text[:200]
        raise PaymentError(f"Razorpay refused the order: {detail or r.status_code}")
    order = r.json()
    db.add(Payment(user_id=user.id, bot_key=bot_key, period=period, currency=currency,
                   amount=amount, order_id=order["id"], status="created"))
    db.flush()
    return {
        "key_id": settings.razorpay_key_id, "order_id": order["id"],
        "amount": amount, "currency": currency,
        "name": settings.platform_name,
        "description": f"{bot.name} - 1 {period}",
        "prefill": {"name": user.name or "", "email": user.email},
    }


def _hmac_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def checkout_signature_ok(order_id: str, payment_id: str, signature: str) -> bool:
    expected = _hmac_hex(settings.razorpay_key_secret, f"{order_id}|{payment_id}".encode())
    return bool(signature) and hmac.compare_digest(expected, signature)


def webhook_signature_ok(body: bytes, signature: str) -> bool:
    if not settings.razorpay_webhook_secret or not signature:
        return False
    return hmac.compare_digest(_hmac_hex(settings.razorpay_webhook_secret, body), signature)


def fulfil(db: Session, order_id: str, provider_payment_id: str,
           amount: int | None = None, currency: str | None = None) -> Subscription | None:
    """Grant access for a paid order. Safe to call twice for the same order."""
    payment = db.execute(
        select(Payment).where(Payment.order_id == order_id).with_for_update()
    ).scalar_one_or_none()
    if payment is None:
        log.warning("payment for unknown order %s", order_id)
        return None
    if payment.status == "paid":
        return db.execute(select(Subscription).where(Subscription.payment_id == payment.id)).scalars().first()
    if amount is not None and (amount != payment.amount or (currency and currency != payment.currency)):
        payment.status = "failed"
        payment.error = f"amount mismatch: paid {amount} {currency}, expected {payment.amount} {payment.currency}"
        db.flush()
        log.error("razorpay %s", payment.error)
        return None
    now = utcnow()
    payment.status, payment.provider_payment_id, payment.paid_at = "paid", provider_payment_id, now
    start = _current_end(db, payment.user_id, payment.bot_key)
    sub = Subscription(user_id=payment.user_id, bot_key=payment.bot_key, kind="paid",
                       period=payment.period, currency=payment.currency, amount=payment.amount,
                       starts_at=start, expires_at=start + dt.timedelta(days=PERIOD_DAYS[payment.period]),
                       payment_id=payment.id)
    db.add(sub)
    db.flush()
    return sub
