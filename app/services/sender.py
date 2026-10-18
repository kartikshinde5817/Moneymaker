"""Sending from a computer other than the server.

Render's free plan blocks outgoing SMTP, so the server only queues approved
emails and `python run.py --send-only` on the user's own computer sends them.
Both read the same database. The PC sender checks in on every pass, so the
dashboard can say whether it is running.
"""

from __future__ import annotations

import datetime as dt
import platform

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import AppState, EmailMessage, as_aware, utcnow

HEARTBEAT_KEY = "pc_sender"
# The sender checks in every minute; a few missed beats means it has stopped.
ALIVE_FOR = dt.timedelta(minutes=3)


def heartbeat(db: Session, sent: int = 0) -> None:
    row = db.get(AppState, HEARTBEAT_KEY)
    if row is None:
        row = AppState(key=HEARTBEAT_KEY, value={})
        db.add(row)
    total = int((row.value or {}).get("sent_total", 0)) + sent
    row.value = {"host": platform.node(), "sent_total": total}
    row.updated_at = utcnow()


def outbox_summary(db: Session) -> dict:
    counts = dict(db.execute(
        select(EmailMessage.status, func.count(EmailMessage.id))
        .where(EmailMessage.status.in_(("queued", "failed", "sending")))
        .group_by(EmailMessage.status)
    ).all())
    beat = db.get(AppState, HEARTBEAT_KEY)
    last = as_aware(beat.updated_at) if beat else None
    return {
        "queued": counts.get("queued", 0) + counts.get("sending", 0),
        "failed": counts.get("failed", 0),
        "pc_sender": {
            "last_seen": last.isoformat() if last else None,
            "alive": bool(last and utcnow() - last < ALIVE_FOR),
            "host": (beat.value or {}).get("host") if beat else None,
        },
    }
