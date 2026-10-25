"""Campaign execution - the stages from ARCHITECTURE.md section 3, in order.

  discover -> resolve -> audit -> extract signals -> qualify -> score
           -> (reject | draft) -> approval queue or send queue

State is checkpointed on the `leads` row at every step, so a crash resumes
rather than restarting, and a scoring change can be replayed over history.

A lead with no signals never reaches the draft stage. That is the rule that
keeps this from being a spam cannon: if we cannot say why we are writing, we
do not write.
"""

from __future__ import annotations

import logging
import threading
import time
import traceback

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models import (
    AIAnalysis, Campaign, CampaignRun, Company, Contact, EmailMessage, Lead,
    SearchQuery, utcnow,
)
from app.services import ai, cities, mailer, notify, ownership, scoring, whatsapp
from app.services.compliance import ALLOWED, is_suppressed
from app.services.discovery import REGISTRY, get_source
from app.services.research import website_finder
from app.services.research.audit import audit_website
from app.services.research.fetcher import registrable_domain
from app.services.resolution import upsert_company, upsert_contact
from app.services.signals import (
    company_signals, get_or_create_source, signals_from_audit, store_raw_signals,
)
from app.models import WebsiteAudit

log = logging.getLogger(__name__)

# The agent graph the dashboard draws. Order is the order data flows.
# `kind` drives the node icon; `engine` says whether a model is involved, which
# the canvas shows so it is obvious where cost and non-determinism live.
PIPELINE_STAGES = [
    ("trigger",     "Trigger",      "Schedule or manual",        "trigger", "system"),
    ("discovery",   "Discovery",    "Query lead sources",        "search",  "api"),
    ("resolution",  "Resolution",   "Merge duplicates",          "merge",   "rules"),
    ("research",    "Research",     "Audit their website",       "globe",   "http"),
    ("signals",     "Evidence",     "Extract quotable proof",    "doc",     "rules"),
    ("qualify",     "Qualify",      "Screen for genuine fit",    "brain",   "llm"),
    ("scoring",     "Scoring",      "Transparent rule score",    "gauge",   "rules"),
    ("personalize", "Personalize",  "Write grounded outreach",   "pen",     "llm"),
    ("outbox",      "Outbox",       "Awaiting your approval",    "inbox",   "system"),
]
STAGE_KEYS = [s[0] for s in PIPELINE_STAGES]


class StageTracker:
    """Live per-node state for the workflow canvas.

    Every number here is produced by work that actually happened. `passed` is
    what left the node, `dropped` is what the node rejected and why - the
    drop-offs matter more than the throughput, because they are where the
    system decides not to contact somebody.
    """

    def __init__(self, run: CampaignRun, db: Session):
        self.run, self.db = run, db
        self.state = {
            key: {
                "key": key, "label": label, "note": note, "icon": icon,
                "engine": engine, "status": "idle", "passed": 0, "dropped": 0,
                "detail": "", "drop_detail": "", "ms": 0,
            }
            for key, label, note, icon, engine in PIPELINE_STAGES
        }
        self._started: dict[str, float] = {}
        # Stages finish in milliseconds, so "currently running" is almost never
        # true when the dashboard polls. Remembering which stage last took
        # control gives the canvas something accurate to highlight.
        self.active: str | None = None
        self.flush()

    def start(self, key: str, detail: str = "") -> None:
        node = self.state[key]
        node["status"] = "running"
        self.active = key
        if detail:
            node["detail"] = detail[:160]
        self._started[key] = time.perf_counter()
        self.flush()

    def done(self, key: str, *, passed: int = 0, dropped: int = 0, detail: str = "") -> None:
        node = self.state[key]
        node["status"] = "done"
        node["passed"] += passed
        node["dropped"] += dropped
        if detail:
            node["detail"] = detail[:160]
        if key in self._started:
            node["ms"] += int((time.perf_counter() - self._started.pop(key)) * 1000)
        self.flush()

    def drop(self, key: str, detail: str = "") -> None:
        """Item rejected at this node - it goes no further down the graph."""
        node = self.state[key]
        node["status"] = "done"
        node["dropped"] += 1
        if detail:
            # Kept apart from `detail` so the drop-off panel shows why something
            # was rejected, not whatever succeeded most recently.
            node["drop_detail"] = detail[:160]
        if key in self._started:
            node["ms"] += int((time.perf_counter() - self._started.pop(key)) * 1000)
        self.flush()

    def skip(self, key: str, detail: str = "") -> None:
        node = self.state[key]
        if node["status"] == "idle":
            node["status"] = "skipped"
            node["detail"] = detail[:160]
        self.flush()

    def fail(self, key: str, detail: str) -> None:
        node = self.state[key]
        node["status"] = "error"
        node["detail"] = detail[:160]
        self.flush()

    def item(self, name: str | None) -> None:
        self.run.current_item = (name or "")[:320] or None
        self.flush()

    def finish(self) -> None:
        for node in self.state.values():
            if node["status"] == "running":
                node["status"] = "done"
        self.active = None
        self.run.current_item = None
        self.flush()

    def flush(self) -> None:
        # Copy each node. Assigning the live dicts would share objects with the
        # previously assigned value, so SQLAlchemy's change detection compares
        # the attribute against itself, sees no difference, and never writes.
        self.run.stage_state = {
            "nodes": [dict(self.state[k]) for k in STAGE_KEYS],
            "active": self.active,
        }
        flag_modified(self.run, "stage_state")
        self.db.flush()


class RunLogger:
    """Collects a readable trace so the dashboard can show what happened."""

    def __init__(self, run: CampaignRun, db: Session):
        self.run, self.db = run, db
        self.lines: list[str] = list(run.log or [])

    def __call__(self, message: str) -> None:
        stamp = utcnow().strftime("%H:%M:%S")
        line = f"{stamp}  {message}"
        self.lines.append(line)
        log.info("[run %s] %s", self.run.id, message)
        self.run.log = list(self.lines[-400:])
        flag_modified(self.run, "log")
        self.db.flush()


class NoAccess(Exception):
    """The campaign's owner no longer has a trial or subscription for its bot."""


class RunCancelled(Exception):
    """Raised at a safe point after the user asks a running campaign to stop."""


# Python threads cannot be killed, so stopping is cooperative: the API records
# the request and the run checks for it between sources and between prospects.
_stop_lock = threading.Lock()
_stop_requested: set[int] = set()


def request_stop(campaign_id: int) -> None:
    with _stop_lock:
        _stop_requested.add(campaign_id)


def stop_requested(campaign_id: int) -> bool:
    with _stop_lock:
        return campaign_id in _stop_requested


def clear_stop(campaign_id: int) -> None:
    with _stop_lock:
        _stop_requested.discard(campaign_id)


def _check_stop(campaign: Campaign) -> None:
    if stop_requested(campaign.id):
        raise RunCancelled()


def close_dead_runs(db: Session, reason: str, campaign_id: int | None = None) -> int:
    """Close runs marked "running" that nothing is executing any more.

    A restart (or Render putting the service to sleep) kills the run's thread
    but leaves its row saying "running" - and the dashboard would show that
    dead run as live forever.
    """
    stmt = select(CampaignRun).where(CampaignRun.status == "running")
    if campaign_id is not None:
        stmt = stmt.where(CampaignRun.campaign_id == campaign_id)
    runs = db.execute(stmt).scalars().all()
    for r in runs:
        r.status = "cancelled"
        r.finished_at = r.finished_at or utcnow()
        r.current_item = None
        r.log = list(r.log or []) + [f"{utcnow():%H:%M:%S}  {reason}"]
    db.commit()
    return len(runs)


def run_campaign(db: Session, campaign: Campaign, *, trigger: str = "manual") -> CampaignRun:
    clear_stop(campaign.id)
    run = CampaignRun(campaign_id=campaign.id, owner_id=campaign.owner_id,
                      trigger=trigger, status="running")
    db.add(run)
    # Committed straight away: the dashboard reads through its own connection
    # and cannot see an uncommitted row, and a rollback on the first prospect
    # would otherwise take the run itself with it.
    db.commit()
    emit = RunLogger(run, db)
    track = StageTracker(run, db)

    stats = {
        "found": 0, "new_companies": 0, "duplicates": 0, "audited": 0,
        "signals": 0, "qualified": 0, "rejected": 0, "leads_created": 0,
        "drafted": 0, "queued": 0, "sent": 0, "errors": 0, "ai_cost_usd": 0.0,
        "whatsapp_queued": 0, "discarded_no_contact": 0,
    }

    from app.services import billing, tenant

    owner_is_admin = tenant.is_admin(db, campaign.owner_id)
    try:
        if not billing.has_access(db, campaign.owner_id, campaign.service_category):
            raise NoAccess()
        emit(f"Campaign '{campaign.name}' starting — service={campaign.service_category}")
        track.start("trigger", f"{trigger} · {campaign.service_category}")
        track.done("trigger", passed=1, detail=f"{trigger} start")

        prospects = _discover(db, campaign, run, emit, stats, track)
        emit(f"Discovery finished: {len(prospects)} prospects from {len(campaign.sources or [])} source(s)")

        for prospect in prospects:
            _check_stop(campaign)
            if stats["leads_created"] >= campaign.daily_lead_limit:
                emit(f"Daily lead limit of {campaign.daily_lead_limit} reached — stopping")
                break
            try:
                track.item(prospect.name)
                _process_prospect(db, campaign, prospect, emit, stats, track)
                db.commit()
            except Exception as exc:  # noqa: BLE001 - one bad prospect must not kill the run
                db.rollback()
                stats["errors"] += 1
                emit(f"  ! {prospect.name}: {type(exc).__name__}: {exc}")
                log.exception("prospect failed")

        run.status = "completed"
        track.finish()
        if notify.ready() and owner_is_admin:
            notify.run_summary(
                campaign=campaign.name, stats=stats,
                drafted=stats["drafted"], sent=stats["sent"], failed=stats["errors"],
            )
        emit(
            f"Done. {stats['leads_created']} leads created, {stats['drafted']} drafted, "
            f"{stats['queued']} queued, {stats['rejected']} rejected, "
            f"{stats['duplicates']} duplicates skipped."
        )
    except NoAccess:
        run.status = "cancelled"
        track.finish()
        emit("Not run: your access to this bot has ended. Renew it under My bots to continue.")
    except RunCancelled:
        run.status = "cancelled"
        track.finish()
        emit(
            f"Stopped by you. {stats['leads_created']} leads and {stats['drafted']} drafts "
            f"made before stopping are kept."
        )
    except Exception as exc:  # noqa: BLE001
        run.status = "failed"
        track.fail("trigger", str(exc)[:160])
        if notify.ready() and owner_is_admin:
            notify.failure_alert(campaign.name, f"{type(exc).__name__}: {exc}")
        run.current_item = None
        run.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[:2000]}"
        emit(f"RUN FAILED: {exc}")
        log.exception("campaign run failed")
    finally:
        clear_stop(campaign.id)
        run.finished_at = utcnow()
        run.stats = stats
        db.commit()
    return run


def _discover(db: Session, campaign: Campaign, run: CampaignRun, emit, stats, track) -> list:
    prospects: list = []
    source_keys = campaign.sources or [k for k, s in REGISTRY.items() if s.available()]
    per_source = max(10, campaign.daily_lead_limit * 3)

    # Fresh ground every run. Searching the same city daily just re-finds
    # businesses already in the database.
    locations = campaign.locations or []
    if campaign.rotate_cities:
        picked, history = cities.pick_cities(
            campaign.countries or ["GB"], campaign.city_history, settings.cities_per_day
        )
        if picked:
            locations = picked
            campaign.city_history = history
            flag_modified(campaign, "city_history")
            cov = cities.coverage(campaign.countries or ["GB"], history)
            emit(f"  Today's cities: {', '.join(picked)} "
                 f"({cov['searched']}/{cov['total']} of the pool searched)")
    track.start("discovery", f"{len(source_keys)} sources · {', '.join(locations) or 'default'}")

    for key in source_keys:
        _check_stop(campaign)
        source = get_source(key)
        if source is None:
            emit(f"  unknown source '{key}' — skipped")
            continue
        if not source.available():
            emit(f"  {source.label}: not configured — skipped")
            continue
        track.start("discovery", f"searching {source.label}")
        emit(f"  {source.label}: searching…")
        result = source.discover(
            service_category=campaign.service_category,
            keywords=campaign.keywords or [],
            countries=campaign.countries or ["GB", "US"],
            locations=locations,
            limit=per_source,
        )
        for query, count in result.queries:
            db.add(SearchQuery(
                run_id=run.id, source=key, query=query[:500],
                results_count=count, cost_usd=result.cost_usd,
            ))
        for err in result.errors:
            stats["errors"] += 1
            emit(f"    error: {err}")
        emit(f"    {len(result.prospects)} prospects")
        prospects.extend(result.prospects)
        stats["found"] += len(result.prospects)
        if result.errors:
            track.fail("discovery", result.errors[0])
        db.commit()  # so the live view shows progress while the other sources run
    if all(n["status"] != "error" for n in [track.state["discovery"]]):
        track.done("discovery", passed=len(prospects),
                   detail=f"{len(prospects)} prospects from {len(source_keys)} sources")
    db.flush()
    return prospects


def _process_prospect(db: Session, campaign: Campaign, prospect, emit, stats, track) -> None:
    track.start("resolution", prospect.name)
    company, created = upsert_company(db, prospect)
    if created:
        stats["new_companies"] += 1
    else:
        stats["duplicates"] += 1

    # One lead per company per service, ever. Re-discovery updates, never duplicates.
    existing = db.execute(
        select(Lead).where(
            Lead.owner_id == campaign.owner_id,
            Lead.company_id == company.id,
            Lead.service_category == campaign.service_category,
        )
    ).scalar_one_or_none()
    if existing and existing.stage not in ("discovered", "researched"):
        emit(f"  = {company.name}: already a lead (stage={existing.stage})")
        track.drop("resolution", f"{company.name} is already a lead")
        return
    track.done("resolution", passed=1,
               detail=f"{company.name} ({'new' if created else 'merged'})")

    src = get_or_create_source(db, prospect.source_kind, prospect.source_url, excerpt=prospect.excerpt)
    if prospect.signals:
        made = store_raw_signals(db, company, prospect.signals, prospect.source_kind)
        stats["signals"] += len(made)

    # Discovery sources hand back portal URLs and helpdesk addresses belonging
    # to somebody else. Auditing those would produce claims about a site the
    # prospect has never seen, so anything not provably theirs is discarded.
    if (company.website and not prospect.extra.get("website_verified")
            and not ownership.domain_belongs_to(company.name, company.website)):
        emit(f"    (ignoring {company.website} — not {company.name}'s own domain)")
        company.website = None
        company.primary_domain = None

    contact = None
    candidates = [prospect.email]
    # A tender body often names the buyer's real address, unlike the portal's.
    candidates += ownership.emails_in_text(prospect.excerpt)
    for candidate in candidates:
        if not candidate:
            continue
        if not ownership.email_belongs_to(company.name, candidate, company.primary_domain):
            continue
        ok, _ = mailer.validate_address(candidate, check_mx=False)
        if ok:
            is_named = candidate == prospect.email
            contact = upsert_contact(
                db, company, candidate,
                name=prospect.contact_name if is_named else None,
                title=prospect.contact_title if is_named else None,
                source_id=src.id, status="valid_syntax",
            )
            break

    # The source named the business but gave no website or address to write to
    # (job ads, tenders, registers). Look for its own site, so the audit can run
    # and an email address can be found. Capped per run to keep runs quick.
    if (not company.website and not contact and prospect.signals and settings.find_websites
            and stats.get("site_lookups", 0) < 30):
        stats["site_lookups"] = stats.get("site_lookups", 0) + 1
        track.start("research", f"looking for {company.name}'s website")
        found = website_finder.find_website(company.name, prospect.country)
        if found:
            company.website = found
            company.primary_domain = registrable_domain(found)
            stats["sites_found"] = stats.get("sites_found", 0) + 1
            emit(f"    found website for {company.name}: {company.primary_domain}")

    # --- website audit --------------------------------------------------
    if company.website:
        track.start("research", f"auditing {company.website}")
        audit = audit_website(company.website, category_hint=prospect.category or "")
        stats["audited"] += 1
        db.add(WebsiteAudit(company_id=company.id, **_audit_columns(audit)))
        made = signals_from_audit(db, company, audit, category=prospect.category)
        stats["signals"] += len(made)
        if not contact and audit.emails_found:
            for found in audit.emails_found:
                if not ownership.email_belongs_to(company.name, found, company.primary_domain):
                    continue
                ok, _ = mailer.validate_address(found, check_mx=False)
                if ok:
                    contact = upsert_contact(
                        db, company, found, source_id=src.id, status="valid_syntax"
                    )
                    break
        if audit.social:
            company.socials = dict(audit.social)
            if audit.social.get("linkedin") and not company.linkedin_url:
                company.linkedin_url = audit.social["linkedin"][:512]
        track.done("research", passed=1, detail=audit.notes or f"audited {audit.url}")
    elif not prospect.signals:
        # No site and no evidence from the source: nothing to say to them.
        emit(f"  - {company.name}: no website and no evidence — skipped")
        track.drop("research", f"{company.name} has no website and no evidence")
        return
    else:
        track.skip("research", "no website on file")

    track.start("signals", company.name)
    signals = company_signals(db, company.id)
    if not signals:
        emit(f"  - {company.name}: no signals found — not a lead")
        track.drop("signals", f"no evidence for {company.name} — refusing to contact")
        return

    # No email and no phone means there is no way to act on this lead, so it is
    # not kept. The website audit above is the last chance to find an address,
    # which is why this check sits here and not earlier.
    if settings.require_contact and not contact and not company.phone:
        emit(f"  - {company.name}: no email or phone — discarded")
        track.drop("signals", f"{company.name} has no email or phone — not stored")
        if created:
            db.delete(company)  # cascades to its signals and audits
            stats["new_companies"] -= 1
            stats["discarded_no_contact"] = stats.get("discarded_no_contact", 0) + 1
        return
    track.done("signals", passed=len(signals),
               detail=f"{len(signals)} signals for {company.name}")

    lead = existing or Lead(
        owner_id=campaign.owner_id, company_id=company.id, campaign_id=campaign.id,
        service_category=campaign.service_category, source_kind=prospect.source_kind,
    )
    lead.contact_id = contact.id if contact else None
    lead.stage = "researched"
    if not existing:
        db.add(lead)
        db.flush()
        stats["leads_created"] += 1

    # --- qualification ----------------------------------------------------
    if not ai.available():
        track.skip("qualify", "free mode — rule-based checks only")
    if ai.available():
        track.start("qualify", company.name)
        try:
            verdict = ai.qualify(company, signals, campaign.service_category)
            usage = verdict.pop("_usage", {})
            db.add(AIAnalysis(
                lead_id=lead.id, agent="qualify", model=usage.get("model", ""),
                input_signal_ids=[s.id for s in signals], output=verdict,
                tokens_in=usage.get("tokens_in", 0), tokens_out=usage.get("tokens_out", 0),
                cost_usd=usage.get("cost_usd", 0.0),
            ))
            stats["ai_cost_usd"] += usage.get("cost_usd", 0.0)
            lead.intent_reason = verdict.get("intent_reason")
            if not verdict.get("is_qualified", True):
                lead.stage = "rejected"
                lead.status = "unqualified"
                lead.notes = verdict.get("disqualify_reason") or "AI qualification rejected"
                stats["rejected"] += 1
                emit(f"  x {company.name}: disqualified — {lead.notes[:90]}")
                track.drop("qualify", f"{company.name}: {lead.notes[:110]}")
                return
            stats["qualified"] += 1
            track.done("qualify", passed=1, detail=f"{company.name} qualified")
        except Exception as exc:  # noqa: BLE001
            emit(f"    (qualification unavailable: {exc}) — falling back to rules")
            track.fail("qualify", str(exc)[:140])

    # --- scoring ----------------------------------------------------------
    track.start("scoring", company.name)
    suppressed = bool(contact and is_suppressed(db, contact.email, campaign.owner_id))
    result = scoring.score_lead(
        company=company, signals=signals, contact=contact,
        service_category=campaign.service_category, suppressed=suppressed,
    )
    lead.score = result.score
    lead.intent_level = result.intent_level
    lead.score_breakdown = result.breakdown
    lead.score_version = scoring.SCORE_VERSION
    lead.stage = "scored"
    if not lead.intent_reason:
        lead.intent_reason = scoring.explain(result.breakdown)

    if result.score < campaign.min_score:
        lead.stage = "rejected"
        lead.status = "below_threshold"
        stats["rejected"] += 1
        emit(f"  x {company.name}: scored {result.score} (below {campaign.min_score})")
        track.drop("scoring", f"{company.name} scored {result.score}, below {campaign.min_score}")
        return

    emit(f"  + {company.name}: scored {result.score} ({result.intent_level}) — {len(signals)} signals")
    track.done("scoring", passed=1, detail=f"{company.name} scored {result.score}")

    # --- draft ------------------------------------------------------------
    if not contact or not contact.email:
        lead.stage = "scored"
        lead.status = "no_contact"
        emit(f"    no email address — kept as a lead, nothing to send")
        track.drop("personalize", f"{company.name}: no email address on file")
        return
    if company.email_policy != ALLOWED:
        lead.status = "manual_outreach"
        emit(f"    {company.country} policy is '{company.email_policy}' — manual outreach only")
        track.drop("personalize",
                   f"{company.country} forbids cold email — {company.name} is manual outreach")
        return

    track.start("personalize", company.name)
    sender = mailer.sender_profile(db, campaign.owner_id)
    draft = ai.draft_email(company, signals, contact, campaign.service_category, sender)
    usage = draft.pop("_usage", {})
    stats["ai_cost_usd"] += usage.get("cost_usd", 0.0)
    db.add(AIAnalysis(
        lead_id=lead.id, agent="personalize", model=usage.get("model", ""),
        input_signal_ids=[s.id for s in signals],
        output={k: v for k, v in draft.items() if not k.startswith("_")},
        tokens_in=usage.get("tokens_in", 0), tokens_out=usage.get("tokens_out", 0),
        cost_usd=usage.get("cost_usd", 0.0),
    ))

    auto = campaign.approval_mode == "auto"
    message = EmailMessage(
        owner_id=campaign.owner_id, lead_id=lead.id, contact_id=contact.id, to_email=contact.email,
        subject=draft["subject"], body=draft["body"],
        cited_signal_ids=draft.get("cited_signal_ids", []),
        unsubscribe_token=mailer.new_unsubscribe_token(),
        status="queued" if auto else "pending_approval",
        scheduled_for=utcnow(),
    )
    db.add(message)
    lead.stage = "queued" if auto else "pending_approval"
    lead.status = "drafted"
    stats["drafted"] += 1
    if auto:
        stats["queued"] += 1
    emit(f"    drafted{' and queued' if auto else ' — awaiting approval'}: {draft['subject'][:60]}")
    track.done("personalize", passed=1,
               detail=("template" if draft.get("_fallback") else "AI") + f": {draft['subject'][:90]}")

    # A phone number means a second channel. Prepared, never auto-sent - see
    # services/whatsapp.py for why that is not optional.
    if settings.whatsapp_enabled and company.phone:
        wa = whatsapp.queue(db, lead, company, signals, sender)
        if wa:
            stats["whatsapp_queued"] = stats.get("whatsapp_queued", 0) + 1
            emit(f"    WhatsApp message prepared for {wa.phone_e164}")
    track.start("outbox", company.name)
    track.done("outbox", passed=1,
               detail=f"{company.name} " + ("queued to send" if auto else "awaiting approval"))


def _audit_columns(audit) -> dict:
    return {
        "url": audit.url, "has_site": audit.has_site, "http_status": audit.http_status,
        "has_ssl": audit.has_ssl, "is_mobile_friendly": audit.is_mobile_friendly,
        "has_booking": audit.has_booking or audit.has_ordering,
        "has_ecommerce": audit.has_ecommerce, "has_contact_form": audit.has_contact_form,
        "copyright_year": audit.copyright_year, "page_bytes": audit.page_bytes,
        "ttfb_ms": audit.ttfb_ms, "tech": audit.tech,
        "emails_found": audit.emails_found, "notes": audit.notes,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def dispatch_queue(db: Session, limit: int = 25, owner_id: int | None = None) -> dict:
    """Send queued messages - every customer's, or one customer's - respecting
    each customer's own mailbox and daily limit. Safe to call on a timer."""
    result = {"sent": 0, "failed": 0, "skipped": 0, "details": []}
    stmt = select(EmailMessage).where(EmailMessage.status == "queued")
    if owner_id is not None:
        stmt = stmt.where(EmailMessage.owner_id == owner_id)
    messages = db.execute(stmt.order_by(EmailMessage.scheduled_for.asc()).limit(limit)).scalars().all()

    # A customer whose limit is reached or whose mail server cannot be
    # reached is skipped for the rest of this pass; the others carry on.
    paused: set[int | None] = set()
    for message in messages:
        if message.owner_id in paused:
            continue
        sent, detail = mailer.send_message(db, message)
        if sent:
            result["sent"] += 1
        elif message.status == "queued":
            paused.add(message.owner_id)
            result["details"].append(detail)
        elif message.status == "rejected":
            result["skipped"] += 1
            result["details"].append(f"{message.to_email}: {detail}")
        else:
            result["failed"] += 1
            result["details"].append(f"{message.to_email}: {detail}")
        db.commit()
    return result
