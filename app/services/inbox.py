"""Reply and bounce detection over IMAP.

Polls the sending mailbox, matches incoming mail back to the message that
provoked it, and acts:

  reply        -> stop the sequence, mark the lead, fire a Telegram alert
  bounce       -> suppress the address permanently so it is never retried
  unsubscribe  -> suppress and close the lead

Matching is done on the In-Reply-To / References headers first, which is
exact, and falls back to the sender address. Without this the system would
keep emailing people who already answered, which is the single most
reputation-damaging thing a cold outreach tool can do.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.utils import parseaddr

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Company, Contact, EmailEvent, EmailMessage, Lead, utcnow,
)
from app.services import notify
from app.services.compliance import suppress

log = logging.getLogger(__name__)

# Bounce and auto-reply senders never represent a human answering.
ROBOT_SENDERS = re.compile(
    r"(mailer-daemon|postmaster|no-?reply|donotreply|bounce|notification)", re.I
)
AUTO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond", "precedence")

UNSUB_WORDS = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt.?out|stop (emailing|contacting)|"
    r"do not (contact|email)|delete my)\b", re.I
)
NEGATIVE_WORDS = re.compile(
    r"\b(not interested|no thanks|no thank you|we('re| are) (all )?(set|good|covered)|"
    r"already have|not (looking|right) (for|now)|please stop|no need)\b", re.I
)
POSITIVE_WORDS = re.compile(
    r"\b(interested|sounds good|tell me more|let's (talk|chat|discuss)|"
    r"book a (call|time)|send (me )?(more|details|examples)|happy to|keen|"
    r"would like to|what (are|is) your (rates|pricing|price))\b", re.I
)
QUESTION_WORDS = re.compile(r"\b(how much|what would|can you|do you|could you|pricing)\b", re.I)

HARD_BOUNCE = re.compile(
    r"(550|551|553|5\.1\.1|5\.1\.10|user unknown|no such user|"
    r"does not exist|mailbox (unavailable|not found)|recipient (rejected|not found)|"
    r"address rejected)", re.I
)


@dataclass
class Inbound:
    from_email: str
    from_name: str
    subject: str
    body: str
    in_reply_to: str
    references: str
    is_auto: bool
    uid: bytes


def ready() -> bool:
    return bool(settings.imap_host and (settings.imap_user or settings.smtp_user))


def _decode(value) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return str(value)


def _plain_body(msg) -> str:
    """Only the new text - quoted history is noise in an alert."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(
                part.get("Content-Disposition", "")
            ):
                try:
                    text = part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace"
                    )
                    break
                except Exception:
                    continue
    else:
        try:
            text = msg.get_payload(decode=True).decode(
                msg.get_content_charset() or "utf-8", "replace"
            )
        except Exception:
            text = str(msg.get_payload())[:4000]

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(">"):
            continue
        if re.match(r"^(on .{10,80}wrote:|-{2,}\s*original message|from:\s)", stripped, re.I):
            break
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def classify(inbound: Inbound) -> str:
    blob = f"{inbound.subject}\n{inbound.body}"
    if HARD_BOUNCE.search(blob) or ROBOT_SENDERS.search(inbound.from_email):
        return "bounce"
    if UNSUB_WORDS.search(blob):
        return "unsubscribe"
    if inbound.is_auto:
        return "auto_reply"
    if NEGATIVE_WORDS.search(blob):
        return "not_interested"
    if POSITIVE_WORDS.search(blob):
        return "interested"
    if QUESTION_WORDS.search(blob):
        return "question"
    return "replied"


def fetch_unseen(limit: int = 40) -> list[Inbound]:
    """Read unseen mail. Messages are marked seen only once handled."""
    host = settings.imap_host
    user = settings.imap_user or settings.smtp_user
    password = settings.imap_pass or settings.smtp_pass
    out: list[Inbound] = []
    try:
        with imaplib.IMAP4_SSL(host, settings.imap_port) as box:
            box.login(user, password)
            box.select(settings.imap_folder)
            status, data = box.search(None, "UNSEEN")
            if status != "OK":
                return out
            uids = data[0].split()[-limit:]
            for uid in uids:
                # BODY.PEEK leaves the message unread if we fail to handle it.
                status, raw = box.fetch(uid, "(BODY.PEEK[])")
                if status != "OK" or not raw or not raw[0]:
                    continue
                msg = email.message_from_bytes(raw[0][1])
                name, addr = parseaddr(_decode(msg.get("From")))
                is_auto = any(msg.get(h) for h in AUTO_HEADERS[:3]) or (
                    (msg.get("precedence") or "").lower() in ("bulk", "auto_reply", "junk")
                )
                out.append(Inbound(
                    from_email=(addr or "").lower(), from_name=name or "",
                    subject=_decode(msg.get("Subject")), body=_plain_body(msg),
                    in_reply_to=(msg.get("In-Reply-To") or "").strip(),
                    references=(msg.get("References") or "").strip(),
                    is_auto=bool(is_auto), uid=uid,
                ))
    except Exception as exc:  # noqa: BLE001
        log.warning("IMAP poll failed: %s: %s", type(exc).__name__, exc)
    return out


def _match_message(db: Session, inbound: Inbound) -> EmailMessage | None:
    """Header match first (exact), then the recipient address."""
    ids = set()
    for raw in (inbound.in_reply_to, inbound.references):
        ids.update(re.findall(r"<[^>]+>", raw or ""))
    for mid in ids:
        hit = db.execute(
            select(EmailMessage).where(EmailMessage.provider_message_id == mid)
        ).scalar_one_or_none()
        if hit:
            return hit
    if inbound.from_email:
        return db.execute(
            select(EmailMessage)
            .where(EmailMessage.to_email == inbound.from_email,
                   EmailMessage.status == "sent")
            .order_by(EmailMessage.sent_at.desc()).limit(1)
        ).scalar_one_or_none()
    return None


def _mark_seen(uids: list[bytes]) -> None:
    if not uids:
        return
    try:
        with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as box:
            box.login(settings.imap_user or settings.smtp_user,
                      settings.imap_pass or settings.smtp_pass)
            box.select(settings.imap_folder)
            for uid in uids:
                box.store(uid, "+FLAGS", "\\Seen")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not mark messages seen: %s", exc)


def poll(db: Session) -> dict:
    """One polling pass. Safe to run on a timer."""
    result = {"checked": 0, "replies": 0, "bounces": 0, "unsubscribes": 0,
              "unmatched": 0, "alerts": 0}
    if not ready():
        result["error"] = "IMAP is not configured"
        return result

    handled: list[bytes] = []
    for inbound in fetch_unseen():
        result["checked"] += 1
        message = _match_message(db, inbound)
        if message is None:
            result["unmatched"] += 1
            continue  # left unread - it is ordinary mail, not ours

        kind = classify(inbound)
        lead = db.get(Lead, message.lead_id)
        company = db.get(Company, lead.company_id) if lead else None
        contact = db.get(Contact, message.contact_id) if message.contact_id else None

        db.add(EmailEvent(
            message_id=message.id,
            type="bounce" if kind == "bounce" else
                 "unsubscribe" if kind == "unsubscribe" else "reply",
            payload={"from": inbound.from_email, "subject": inbound.subject,
                     "sentiment": kind, "snippet": inbound.body[:1000]},
        ))

        if kind == "bounce":
            suppress(db, message.to_email, "hard bounce")
            if lead:
                lead.status, lead.stage = "bounced", "closed"
            result["bounces"] += 1
        elif kind == "unsubscribe":
            suppress(db, message.to_email, "asked to be removed by reply")
            if lead:
                lead.status, lead.stage = "unsubscribed", "closed"
            result["unsubscribes"] += 1
        else:
            # Any human answer stops the sequence. Following up on someone who
            # already replied is the fastest way to lose them.
            if lead:
                lead.status = kind
                lead.stage = "replied"
                lead.next_action_at = None
            result["replies"] += 1

        if kind not in ("bounce", "auto_reply") and company:
            ok, _ = notify.reply_alert(
                company=company.name,
                contact=contact.full_name if contact else inbound.from_name,
                from_email=inbound.from_email, subject=inbound.subject,
                snippet=inbound.body, sentiment=kind,
                lead_url=f"{settings.public_base_url.rstrip('/')}/#lead-{lead.id}" if lead else "",
                service=lead.service_category if lead else "",
                score=lead.score if lead else None,
            )
            if ok:
                result["alerts"] += 1

        handled.append(inbound.uid)
        db.commit()

    _mark_seen(handled)
    return result
