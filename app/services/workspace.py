"""Moving the admin's own workspace into a customer account.

The admin account runs the platform: customers, bots, prices and payments.
Before the app had customers, the admin also ran the agents, so its account
still owns campaigns, leads, emails and an unsubscribe list. This hands all
of that to a normal customer account, once, so the admin panel stays a pure
control panel and the work carries on under the new account.

The admin's mailbox and sender details came from the environment. A customer
has no environment fallback, so those values are copied into the customer's
own settings (passwords encrypted with SECRET_KEY) and sending keeps working.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AIAnalysis, AccountSettings, Campaign, CampaignRun, EmailMessage, Lead, Mailbox, Payment,
    Session as LoginSession, Subscription, Suppression, User, WhatsAppMessage,
)
from app.services import auth, billing, tenant

# Everything a workspace owns, in an order that keeps foreign keys happy.
OWNED = (Campaign, CampaignRun, Lead, EmailMessage, WhatsAppMessage, Mailbox, Suppression)

# The owner's own account keeps its bots for good.
OWNER_GRANT_DAYS = 3650


def counts(db: Session, owner_id: int) -> dict:
    return {m.__tablename__: db.execute(select(func.count()).select_from(m)
                                        .where(m.owner_id == owner_id)).scalar_one()
            for m in OWNED}


def _copy_mail_settings(db: Session, admin: int, target: int) -> None:
    """Give the customer the sending setup the admin was using, where unset."""
    src = tenant.config_for(db, admin)
    row = db.get(AccountSettings, target)
    if row is None:
        row = AccountSettings(owner_id=target)
        db.add(row)
    plain = {
        "sender_name": src.sender_name, "sender_email": src.sender_email,
        "company_name": src.company_name, "company_website": src.company_website,
        "postal_address": src.postal_address, "privacy_url": src.privacy_url,
        "smtp_host": src.smtp_host, "smtp_port": src.smtp_port, "smtp_use_ssl": src.smtp_use_ssl,
        "smtp_user": src.smtp_user, "imap_host": src.imap_host, "imap_port": src.imap_port,
        "imap_user": src.imap_user, "imap_folder": src.imap_folder,
        "daily_cap": src.daily_cap, "timezone": src.timezone,
    }
    for field, value in plain.items():
        if getattr(row, field) in (None, "") and value not in (None, ""):
            setattr(row, field, value)
    if not row.smtp_pass_enc and src.smtp_pass:
        row.smtp_pass_enc = tenant.encrypt(src.smtp_pass)
    if not row.imap_pass_enc and src.imap_pass:
        row.imap_pass_enc = tenant.encrypt(src.imap_pass)


def transfer_admin_workspace(db: Session, *, email: str, password: str = "",
                             name: str = "") -> dict:
    """Move every row the admin owns to the customer with this email.

    Creates the customer when the email is new. An existing customer keeps its
    own data; where both own the same lead or unsubscribe entry, the
    customer's copy wins and the admin's duplicate is dropped.
    """
    email = email.strip().lower()
    admin = tenant.admin_id(db)
    if email == (settings.admin_email or "").strip().lower():
        raise ValueError("Choose an email other than the admin's own.")

    user = db.execute(select(User).where(func.lower(User.email) == email)).scalar_one_or_none()
    created = user is None
    if created:
        if len(password) < 8:
            raise ValueError("Set a password of at least 8 characters for the new account.")
        user = auth.create_customer(db, email=email, password=password,
                                    name=name or email.split("@")[0])
        from app.models import utcnow
        user.email_verified_at = utcnow()  # the admin is vouching for this address
    elif user.role != "customer":
        raise ValueError("That email belongs to the admin account.")
    elif password:
        if len(password) < 8:
            raise ValueError("The password must be at least 8 characters.")
        user.password_hash = auth.hash_password(password)
    target = user.id

    # Rows the customer already has would break the per-owner unique keys:
    # the customer's lead wins, and the admin's emails move onto it.
    mine = {(l.company_id, l.service_category): l.id for l in db.execute(
        select(Lead).where(Lead.owner_id == target)).scalars()}
    remap = {l.id: mine[(l.company_id, l.service_category)] for l in db.execute(
        select(Lead).where(Lead.owner_id == admin)).scalars()
        if (l.company_id, l.service_category) in mine}
    for old, new in remap.items():
        for model in (EmailMessage, WhatsAppMessage, AIAnalysis):
            db.execute(update(model).where(model.lead_id == old).values(lead_id=new))
    if remap:
        db.execute(delete(Lead).where(Lead.id.in_(list(remap))))
    have = {(s.scope, s.value) for s in db.execute(
        select(Suppression).where(Suppression.owner_id == target)).scalars()}
    db.execute(delete(Suppression).where(Suppression.owner_id == admin, Suppression.id.in_(
        [s.id for s in db.execute(select(Suppression).where(Suppression.owner_id == admin)).scalars()
         if (s.scope, s.value) in have])))

    before = counts(db, admin)
    _copy_mail_settings(db, admin, target)
    for model in OWNED:
        db.execute(update(model).where(model.owner_id == admin).values(owner_id=target))

    # Access to every bot the moved agents use, so nothing is locked.
    bots = {c.service_category for c in db.execute(
        select(Campaign).where(Campaign.owner_id == target)).scalars()}
    live = billing.access_map(db, target)
    for key in sorted(bots):
        if key not in live:
            billing.grant(db, target, key, OWNER_GRANT_DAYS, "Platform owner's workspace")
    db.commit()

    from app.scheduler import reschedule
    reschedule()
    return {"user_id": target, "email": user.email, "created": created, "moved": before,
            "bots": sorted(bots)}


def delete_account(db: Session, user: User) -> dict:
    """Remove a customer and everything that belongs to them, for good.

    Their agents, runs, leads, drafts and sent mail, WhatsApp queue, mailboxes,
    unsubscribe list, mail settings, plans, payments and sign-ins all go.
    Companies, contacts and evidence are shared facts about businesses, not the
    customer's data, so they stay for everyone else.

    Rows are deleted child first so it works the same on SQLite and Postgres,
    whatever the database does with cascades.
    """
    uid = user.id
    if user.role != "customer" or uid == tenant.admin_id(db):
        raise ValueError("Only user accounts can be deleted.")
    gone = counts(db, uid)
    own = lambda m: m.owner_id == uid  # noqa: E731

    lead_ids = select(Lead.id).where(Lead.owner_id == uid)
    db.execute(delete(AIAnalysis).where(AIAnalysis.lead_id.in_(lead_ids)))
    for model in (EmailMessage, WhatsAppMessage):
        db.execute(delete(model).where(model.lead_id.in_(lead_ids)))
    for model in (EmailMessage, WhatsAppMessage):  # anything left that carries the owner id
        db.execute(delete(model).where(own(model)))
    db.execute(delete(Lead).where(own(Lead)))
    db.execute(delete(CampaignRun).where(own(CampaignRun)))
    db.execute(delete(Campaign).where(own(Campaign)))
    db.execute(delete(Mailbox).where(own(Mailbox)))
    db.execute(delete(Suppression).where(own(Suppression)))
    db.execute(delete(AccountSettings).where(AccountSettings.owner_id == uid))
    db.execute(delete(Subscription).where(Subscription.user_id == uid))
    db.execute(delete(Payment).where(Payment.user_id == uid))
    db.execute(delete(LoginSession).where(LoginSession.user_id == uid))
    db.delete(user)
    db.commit()
    auth._forget_sessions()  # the 60 s session cache must not keep them signed in
    return gone
