"""Campaign scheduling.

Schedules come from the `campaigns` table, so changing a time in the dashboard
takes effect without a restart. Two safeguards matter:

  * A campaign already running is never started again. A slow run must not
    overlap its own next trigger.
  * Discovery and sending are on separate timers. Campaigns research at their
    scheduled hour; the dispatcher releases mail separately, so nothing is
    emailed at 07:00 local-to-us in the middle of a prospect's night.
"""

from __future__ import annotations

import datetime as dt
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.db import session_scope
from app.models import AccountSettings, Campaign, User
from app.services import inbox, pipeline

log = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None

DAY_MAP = {
    "mon-fri": "mon-fri",
    "daily": "*",
    "weekends": "sat,sun",
}


def _run(campaign_id: int) -> None:
    from app.api import _running, _running_lock

    with _running_lock:
        if campaign_id in _running:
            log.warning("campaign %s already running - skipping this trigger", campaign_id)
            return
        _running.add(campaign_id)
    try:
        with session_scope() as db:
            campaign = db.get(Campaign, campaign_id)
            owner = db.get(User, campaign.owner_id) if campaign and campaign.owner_id else None
            if owner is not None and owner.status != "active":
                log.info("campaign %s skipped: owner account is %s", campaign_id, owner.status)
                return
            if campaign and campaign.enabled:
                pipeline.run_campaign(db, campaign, trigger="scheduled")
    except Exception:
        log.exception("scheduled campaign %s failed", campaign_id)
    finally:
        with _running_lock:
            _running.discard(campaign_id)


def _poll_inbox() -> None:
    """Replies, bounces and opt-outs. Runs often - a follow-up sent after
    somebody already answered is the worst outcome this system can produce."""
    try:
        with session_scope() as db:
            result = inbox.poll_all(db)
            if result.get("replies") or result.get("bounces") or result.get("unsubscribes"):
                log.info("inbox: %s", result)
    except Exception:
        log.exception("inbox poll failed")


def _dispatch() -> None:
    if not settings.send_on_server:
        return  # the PC sender (run.py --send-only) sends the queue
    try:
        with session_scope() as db:
            result = pipeline.dispatch_queue(db, limit=20)
            if result["sent"] or result["failed"]:
                log.info("dispatcher: %s", result)
    except Exception:
        log.exception("dispatcher failed")


def _trigger(c: Campaign, timezone: str | None = None) -> CronTrigger:
    """Fires at the campaign's time in its owner's own timezone."""
    hour, minute = (c.schedule_time or "07:00").split(":")
    try:
        return CronTrigger(
            hour=int(hour), minute=int(minute),
            day_of_week=DAY_MAP.get(c.schedule_days, "mon-fri"),
            timezone=timezone or settings.scheduler_timezone,
        )
    except Exception:  # noqa: BLE001 - an unknown timezone falls back to the platform's
        return CronTrigger(hour=int(hour), minute=int(minute),
                           day_of_week=DAY_MAP.get(c.schedule_days, "mon-fri"),
                           timezone=settings.scheduler_timezone)


def _timezones(db: Session) -> dict[int, str]:
    return {o: tz for o, tz in db.execute(
        select(AccountSettings.owner_id, AccountSettings.timezone)
        .where(AccountSettings.timezone.isnot(None))).all() if tz}


def upcoming(db: Session, campaigns: list[Campaign] | None = None) -> list[dict]:
    """Next start of every enabled campaign, soonest first.

    Read from the live job where one exists, so the dashboard shows exactly
    what the scheduler will do. With the scheduler off, the same trigger is
    evaluated directly - the times are still right, they just will not fire.
    """
    if campaigns is None:
        campaigns = db.execute(select(Campaign).where(Campaign.enabled.is_(True))).scalars().all()
    zones = _timezones(db)
    out = []
    for c in (c for c in campaigns if c.enabled):
        job = _scheduler.get_job(f"campaign:{c.id}") if _scheduler else None
        at = job.next_run_time if job else None
        if at is None:
            try:
                trigger = _trigger(c, zones.get(c.owner_id))
                at = trigger.get_next_fire_time(None, dt.datetime.now(trigger.timezone))
            except (ValueError, TypeError):
                continue
        if at is None:
            continue
        out.append((at, {
            "campaign_id": c.id, "name": c.name, "service": c.service_category,
            "schedule_time": c.schedule_time, "schedule_days": c.schedule_days,
            "at": at.isoformat(),
        }))
    return [row for _, row in sorted(out, key=lambda pair: pair[0])]


def reschedule() -> None:
    """Rebuild every campaign job from the database."""
    if _scheduler is None:
        return
    for job in _scheduler.get_jobs():
        if job.id.startswith("campaign:"):
            job.remove()
    with session_scope() as db:
        campaigns = db.execute(select(Campaign).where(Campaign.enabled.is_(True))).scalars().all()
        zones = _timezones(db)
        for c in campaigns:
            try:
                _scheduler.add_job(
                    _run, _trigger(c, zones.get(c.owner_id)),
                    id=f"campaign:{c.id}", args=[c.id],
                    replace_existing=True, misfire_grace_time=3600,
                    coalesce=True, max_instances=1,
                )
                log.info("scheduled '%s' at %s %s", c.name, c.schedule_time, settings.scheduler_timezone)
            except (ValueError, TypeError) as exc:
                log.warning("bad schedule on campaign %s (%s): %s", c.id, c.schedule_time, exc)


def start() -> BackgroundScheduler | None:
    global _scheduler
    if not settings.scheduler_enabled:
        log.info("scheduler disabled by configuration")
        return None
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone=settings.scheduler_timezone)
    _scheduler.add_job(
        _dispatch, CronTrigger(minute="*/10", timezone=settings.scheduler_timezone),
        id="dispatcher", replace_existing=True, max_instances=1, coalesce=True,
    )
    _scheduler.add_job(
        _poll_inbox, CronTrigger(minute="*/5", timezone=settings.scheduler_timezone),
        id="inbox", replace_existing=True, max_instances=1, coalesce=True,
    )
    reschedule()
    _scheduler.start()
    log.info("scheduler started (%s)", settings.scheduler_timezone)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def jobs() -> list[dict]:
    if _scheduler is None:
        return []
    return [{
        "id": j.id, "name": str(j.func_ref or j.id),
        "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
    } for j in _scheduler.get_jobs()]
