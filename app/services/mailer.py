"""Email validation, assembly and sending.

Deliverability is what decides whether any of this works, so the gates here are
deliberately strict. A single day of sending to dead addresses will get a new
domain throttled for weeks.

Every outbound message carries:
  * a real postal address        - required by CAN-SPAM
  * List-Unsubscribe + one-click - RFC 8058, and expected by Gmail/Yahoo
  * a plain-text footer opt-out
  * honest headers

Nothing is sent unless the recipient's country policy allows cold email, the
address passes validation, and the company has not been contacted inside the
cooling-off window.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
import smtplib
import socket
from dataclasses import dataclass
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

import dns.resolver
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Company, Contact, EmailEvent, EmailMessage, Lead, Mailbox, as_aware, utcnow,
)
from app.services.compliance import (
    ALLOWED, is_disposable, is_never_email, is_suppressed, suppress,
)

log = logging.getLogger(__name__)

_mx_cache: dict[str, bool] = {}


@dataclass
class SendGate:
    ok: bool
    reason: str = ""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def has_mx(domain: str) -> bool:
    """Does this domain accept mail at all? Cheapest way to avoid hard bounces."""
    domain = (domain or "").lower().strip()
    if not domain:
        return False
    if domain in _mx_cache:
        return _mx_cache[domain]
    ok = False
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=6)
        ok = len(answers) > 0
    except Exception:
        try:  # a bare A record still accepts mail per RFC 5321
            dns.resolver.resolve(domain, "A", lifetime=4)
            ok = True
        except Exception:
            ok = False
    _mx_cache[domain] = ok
    return ok


def validate_address(email: str, *, check_mx: bool = True) -> tuple[bool, str]:
    from email_validator import EmailNotValidError, validate_email

    email = (email or "").strip().lower()
    if not email:
        return False, "no address"
    try:
        validate_email(email, check_deliverability=False)
    except EmailNotValidError as exc:
        return False, f"invalid syntax: {exc}"
    if is_never_email(email):
        return False, "role address that must never receive outreach"
    if is_disposable(email):
        return False, "disposable address"
    if check_mx and not has_mx(email.split("@", 1)[1]):
        return False, "domain does not accept mail"
    return True, "ok"


def sent_recently(
    db: Session, company_id: int, days: int | None = None,
    exclude_message_id: int | None = None, owner_id: int | None = None,
) -> bool:
    """Cooling-off. Being found by three sources must not mean three emails.

    `exclude_message_id` is the message being sent right now. Without it the
    check counts that message as a previous contact and blocks every send.
    """
    days = days if days is not None else settings.cooling_off_days
    cutoff = utcnow() - dt.timedelta(days=days)
    q = (
        select(func.count(EmailMessage.id))
        .join(Lead, Lead.id == EmailMessage.lead_id)
        .where(
            Lead.company_id == company_id,
            EmailMessage.status.in_(("sent", "sending")),
            EmailMessage.created_at >= cutoff,
        )
    )
    if owner_id is not None:
        # Cooling-off is per customer: another customer's email is not ours.
        q = q.where(EmailMessage.owner_id == owner_id)
    if exclude_message_id is not None:
        q = q.where(EmailMessage.id != exclude_message_id)
    return db.execute(q).scalar_one() > 0


def sent_today(db: Session, mailbox_id: int | None = None, owner_id: int | None = None) -> int:
    start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    q = select(func.count(EmailMessage.id)).where(
        EmailMessage.status == "sent", EmailMessage.sent_at >= start
    )
    if mailbox_id:
        q = q.where(EmailMessage.mailbox_id == mailbox_id)
    if owner_id is not None:
        q = q.where(EmailMessage.owner_id == owner_id)
    return db.execute(q).scalar_one()


# Day-of-warmup -> daily ceiling. A new domain that sends 100 on day one gets
# filtered before anyone reads it; the ramp is what builds sending reputation.
WARMUP_SCHEDULE = ((7, 8), (14, 20), (21, 30), (30, 50))


def warmup_cap(mailbox: Mailbox) -> int:
    """Ramp a new mailbox instead of blasting from day one."""
    if not mailbox.warmup_started_at:
        # Never sent: still day zero, not a seasoned mailbox.
        return min(mailbox.daily_cap, WARMUP_SCHEDULE[0][1])
    days = (utcnow() - as_aware(mailbox.warmup_started_at)).days
    for day_limit, cap in WARMUP_SCHEDULE:
        if days < day_limit:
            return min(mailbox.daily_cap, cap)
    return mailbox.daily_cap


def check_can_send(
    db: Session, lead: Lead, company: Company, contact: Contact | None,
    exclude_message_id: int | None = None,
) -> SendGate:
    """Every reason this email must never go out, judged for the customer who
    owns the lead: their unsubscribe list and their cooling-off history.

    The daily limit is not here - being over it only means "not today", so
    send_message keeps such an email queued instead of rejecting it.
    """
    owner = lead.owner_id
    if not contact or not contact.email:
        return SendGate(False, "no email address on file")
    if company.email_policy != ALLOWED:
        return SendGate(False, f"country policy for {company.country} is '{company.email_policy}'")
    reason = is_suppressed(db, contact.email, owner)
    if reason:
        return SendGate(False, f"suppressed: {reason}")
    ok, why = validate_address(contact.email)
    if not ok:
        return SendGate(False, why)
    if sent_recently(db, company.id, exclude_message_id=exclude_message_id, owner_id=owner):
        return SendGate(False, f"already contacted within {settings.cooling_off_days} days")
    return SendGate(True, "ok")


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def new_unsubscribe_token() -> str:
    return secrets.token_urlsafe(24)


def sender_profile(db: Session | None = None, owner_id: int | None = None) -> dict:
    """The sending identity for a customer (or, with no customer, the env's)."""
    if db is not None and owner_id is not None:
        from app.services.tenant import config_for
        return config_for(db, owner_id).sender()
    return {
        "sender_name": settings.sender_name or "",
        "sender_email": settings.sender_email or "",
        "company_name": settings.company_name or "",
        "company_website": settings.company_website or "",
        "company_postal_address": settings.company_postal_address or "",
        "portfolio_url": settings.portfolio_url or "",
        "privacy_policy_url": settings.privacy_policy_url or "",
    }


def public_url_is_reachable() -> bool:
    """Is PUBLIC_BASE_URL something a recipient could actually open?"""
    url = (settings.public_base_url or "").strip().lower()
    if not url.startswith(("http://", "https://")):
        return False
    host = url.split("://", 1)[1].split("/")[0].split(":")[0]
    return host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1") and "." in host


def unsubscribe_url(token: str) -> str:
    return f"{settings.public_base_url.rstrip('/')}/u/{token}"


def build_footer(token: str, sender: dict) -> str:
    """The compliance footer. Not optional in any jurisdiction we send to."""
    url = unsubscribe_url(token)
    lines = ["", "—"]
    if sender.get("company_name"):
        lines.append(sender["company_name"])
    if sender.get("company_postal_address"):
        lines.append(sender["company_postal_address"])
    lines.append(f"Not interested? Unsubscribe here and I won't contact you again: {url}")
    if sender.get("privacy_policy_url"):
        lines.append(f"How we handle your data: {sender['privacy_policy_url']}")
    return "\n".join(lines)


def compose(message: EmailMessage, sender: dict) -> MIMEMultipart:
    from app.services.ai import render_signature

    token = message.unsubscribe_token or new_unsubscribe_token()
    sig_sender = dict(sender)
    if sig_sender.get("company_website") and sig_sender["company_website"] in message.body:
        sig_sender["company_website"] = ""
    body = message.body.rstrip() + render_signature(sig_sender) + "\n" + build_footer(token, sender)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = message.subject
    msg["From"] = formataddr((sender.get("sender_name") or "", sender.get("sender_email") or ""))
    msg["To"] = message.to_email
    msg["Message-ID"] = make_msgid()
    msg["List-Unsubscribe"] = (
        f"<{unsubscribe_url(token)}>, <mailto:{sender.get('sender_email')}?subject=unsubscribe>"
    )
    # RFC 8058 - lets Gmail and Yahoo show a native one-click unsubscribe.
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    msg.attach(MIMEText(body, "plain", "utf-8"))
    return msg


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------


class SMTPProvider:
    key = "smtp"

    def available(self) -> bool:
        # Auth is optional: a local relay or an IP-allowlisted server needs
        # no credentials, and send() only logs in when a user is configured.
        return bool(settings.smtp_host)

    def send(self, msg: MIMEMultipart, to_email: str, from_email: str, cfg=None) -> str:
        host = cfg.smtp_host if cfg else settings.smtp_host
        port = cfg.smtp_port if cfg else settings.smtp_port
        use_ssl = cfg.smtp_use_ssl if cfg else settings.smtp_use_ssl
        user = cfg.smtp_user if cfg else settings.smtp_user
        password = cfg.smtp_pass if cfg else settings.smtp_pass
        try:
            if use_ssl or port == 465:
                server = smtplib.SMTP_SSL(host, port, timeout=30)
            else:
                server = smtplib.SMTP(host, port, timeout=30)
                server.ehlo()
                if server.has_extn("starttls"):
                    server.starttls()
                    server.ehlo()
                elif user:
                    # Never hand credentials to a server that cannot encrypt.
                    raise RuntimeError(
                        f"{host}:{port} does not support STARTTLS; refusing to "
                        f"send credentials in clear text"
                    )
            with server:
                if user:
                    server.login(user, password)
                server.sendmail(from_email, [to_email], msg.as_string())
        except (smtplib.SMTPException, socket.error, OSError) as exc:
            raise RuntimeError(f"SMTP send failed: {type(exc).__name__}: {exc}") from exc
        return msg["Message-ID"]


PROVIDERS = {"smtp": SMTPProvider()}

UNREACHABLE_HINT = (
    "This server could not reach the mail server, so nothing was sent. The email "
    "is approved and waiting in the queue. On Render's free plan email ports are "
    "blocked: set SEND_ON_SERVER=false there and run `python run.py --send-only` "
    "on your computer."
)


def _never_reached_server(exc: Exception) -> bool:
    """True when the connection itself failed, so the email certainly was not sent.

    Errors from a mail server that was reached (wrong password, refused
    recipient) are real failures. A dropped connection mid-send is not
    counted here, since the message might already have been accepted.
    """
    cause = exc.__cause__ or exc
    if isinstance(cause, smtplib.SMTPServerDisconnected):
        return False
    return isinstance(cause, (ConnectionError, TimeoutError, socket.gaierror,
                              smtplib.SMTPConnectError)) or type(cause) is OSError


def active_provider():
    p = PROVIDERS["smtp"]
    return p if p.available() else None


def provider_ready(cfg=None) -> bool:
    if cfg is not None:
        return cfg.smtp_ready
    return active_provider() is not None


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def mailbox_for(db: Session, owner_id: int, cfg) -> Mailbox | None:
    """The customer's sending mailbox, created on first use so warm-up applies."""
    box = db.execute(
        select(Mailbox).where(Mailbox.owner_id == owner_id, Mailbox.enabled.is_(True))
        .order_by(Mailbox.last_sent_at.asc())
    ).scalars().first()
    if box or not cfg.sender_email:
        return box
    taken = db.execute(select(Mailbox).where(Mailbox.address == cfg.sender_email.lower())).scalar_one_or_none()
    if taken:
        return None if taken.owner_id != owner_id else taken
    box = Mailbox(owner_id=owner_id, address=cfg.sender_email.lower(),
                  display_name=cfg.sender_name or "", daily_cap=cfg.daily_cap)
    db.add(box)
    db.flush()
    return box


def record_event(db: Session, message: EmailMessage, kind: str, payload: dict | None = None) -> None:
    db.add(EmailEvent(message_id=message.id, type=kind, payload=payload or {}))


def send_message(db: Session, message: EmailMessage) -> tuple[bool, str]:
    """Send one queued message. Returns (sent, detail). Never raises."""
    lead = db.get(Lead, message.lead_id)
    company = db.get(Company, lead.company_id) if lead else None
    contact = db.get(Contact, message.contact_id) if message.contact_id else None
    if not lead or not company:
        return False, "lead or company missing"

    gate = check_can_send(db, lead, company, contact, exclude_message_id=message.id)
    if not gate.ok:
        message.status = "rejected"
        message.failure_reason = gate.reason
        record_event(db, message, "failed", {"reason": gate.reason})
        return False, gate.reason

    from app.services.tenant import config_for

    owner = message.owner_id or lead.owner_id
    cfg = config_for(db, owner)
    provider = PROVIDERS["smtp"]
    if not cfg.smtp_ready:
        message.failure_reason = "no mailbox connected - add your SMTP details in Settings"
        record_event(db, message, "failed", {"reason": message.failure_reason})
        return False, message.failure_reason

    sender = cfg.sender()
    if not sender.get("sender_email"):
        message.failure_reason = "Sender email is not set - add it in Settings"
        return False, message.failure_reason
    if not sender.get("company_postal_address"):
        # CAN-SPAM requires this. Refuse rather than send unlawfully.
        message.failure_reason = "A postal address is required before sending - add it in Settings"
        return False, message.failure_reason
    if not public_url_is_reachable():
        # Every message carries an unsubscribe link built from PUBLIC_BASE_URL.
        # Pointing it at localhost means the recipient's opt-out silently fails,
        # which defeats the one protection they have - and a working opt-out is
        # required by CAN-SPAM, PECR and the rest. Refuse rather than pretend.
        message.failure_reason = (
            "PUBLIC_BASE_URL is still localhost, so the unsubscribe link would "
            "not work for the recipient. Set it to a public URL before sending."
        )
        return False, message.failure_reason

    if sent_today(db, owner_id=owner) >= cfg.daily_cap:
        message.status = "queued"
        return False, f"Daily sending limit of {cfg.daily_cap} reached - it will go out tomorrow."

    if not message.unsubscribe_token:
        message.unsubscribe_token = new_unsubscribe_token()

    mailbox = mailbox_for(db, owner, cfg)
    if mailbox:
        cap = warmup_cap(mailbox)
        if sent_today(db, mailbox.id) >= cap:
            message.status = "queued"
            return False, (f"{mailbox.address} is warming up and has reached today's limit of "
                           f"{cap} - it will go out tomorrow.")
        message.mailbox_id = mailbox.id

    message.status = "sending"
    db.flush()
    try:
        mime = compose(message, sender)
        provider_id = provider.send(mime, message.to_email, sender["sender_email"], cfg)
    except Exception as exc:  # noqa: BLE001
        if _never_reached_server(exc):
            # Nothing left this server, so the approval stands: keep it queued
            # for the next attempt (or for the PC sender) instead of failing it.
            message.status = "queued"
            message.failure_reason = f"Could not reach the mail server: {exc}"[:500]
            return False, UNREACHABLE_HINT
        message.status = "failed"
        message.failure_reason = str(exc)[:500]
        record_event(db, message, "failed", {"reason": message.failure_reason})
        low = str(exc).lower()
        # A hard rejection means the address is dead - never try it again.
        if any(k in low for k in ("550", "no such user", "does not exist", "unknown recipient")):
            # Dead for everyone, so platform-wide.
            suppress(db, message.to_email, "hard bounce at send time")
        return False, message.failure_reason

    now = utcnow()
    message.status = "sent"
    message.provider_message_id = provider_id
    message.sent_at = now
    message.failure_reason = None
    record_event(db, message, "sent", {"provider_message_id": provider_id})

    lead.status = "contacted"
    lead.stage = "sent"
    lead.last_contact_at = now
    if mailbox:
        mailbox.last_sent_at = now
        if not mailbox.warmup_started_at:
            mailbox.warmup_started_at = now
    return True, "sent"
