"""Who owns what, and each customer's own sending setup.

Every customer works on their own data: campaigns, leads, emails, the
unsubscribe list and the mailbox they send from. Companies, contacts and the
evidence about them are public facts and are shared, so two customers looking
at the same business do not pay for the same research twice.

The admin is the platform owner and also a user of the bots. For the admin,
empty mailbox and sender fields fall back to the environment variables the
app used before it had customers, so an existing setup keeps working.
"""

from __future__ import annotations

import base64
import hashlib
import threading
from dataclasses import dataclass

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AccountSettings, User

CUSTOMER_DEFAULT_DAILY_CAP = 50

_admin_lock = threading.Lock()
_admin_id: int | None = None


def admin_id(db: Session) -> int:
    global _admin_id
    with _admin_lock:
        if _admin_id is not None:
            return _admin_id
    uid = db.execute(
        select(User.id).where(User.role == "admin").order_by(User.id)
    ).scalars().first()
    if uid is None:
        from app.services.auth import admin_user
        uid = admin_user(db).id
    with _admin_lock:
        _admin_id = uid
    return uid


def is_admin(db: Session, owner_id: int) -> bool:
    return owner_id == admin_id(db)


# --- secrets at rest ----------------------------------------------------------

def _fernet() -> Fernet:
    key = hashlib.sha256(f"moneymaker:{settings.secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str | None) -> str | None:
    return _fernet().encrypt(value.encode()).decode() if value else None


def decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError):
        # SECRET_KEY changed since this was saved: the customer re-enters it.
        return ""


# --- per-customer configuration -------------------------------------------------

@dataclass
class MailConfig:
    owner_id: int
    is_admin: bool
    sender_name: str = ""
    sender_email: str = ""
    company_name: str = ""
    company_website: str = ""
    postal_address: str = ""
    privacy_url: str = ""
    portfolio_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_use_ssl: bool = True
    smtp_user: str = ""
    smtp_pass: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_pass: str = ""
    imap_folder: str = "INBOX"
    daily_cap: int = CUSTOMER_DEFAULT_DAILY_CAP
    timezone: str = "Asia/Kolkata"

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host)

    @property
    def imap_ready(self) -> bool:
        return bool(self.imap_host and (self.imap_user or self.smtp_user))

    @property
    def sender_ready(self) -> bool:
        return bool(self.sender_email and self.postal_address)

    def sender(self) -> dict:
        """The dict shape the email writer and footer have always used."""
        return {
            "sender_name": self.sender_name,
            "sender_email": self.sender_email,
            "company_name": self.company_name,
            "company_website": self.company_website,
            "company_postal_address": self.postal_address,
            "portfolio_url": self.portfolio_url,
            "privacy_policy_url": self.privacy_url,
        }


def config_for(db: Session, owner_id: int) -> MailConfig:
    row = db.get(AccountSettings, owner_id)
    admin = is_admin(db, owner_id)

    def pick(field: str, env_value):
        value = getattr(row, field, None) if row else None
        if value not in (None, ""):
            return value
        return env_value if admin else None

    s = settings
    cfg = MailConfig(owner_id=owner_id, is_admin=admin)
    cfg.sender_name = pick("sender_name", s.sender_name) or ""
    cfg.sender_email = pick("sender_email", s.sender_email) or ""
    cfg.company_name = pick("company_name", s.company_name) or ""
    cfg.company_website = pick("company_website", s.company_website if admin else "") or ""
    cfg.postal_address = pick("postal_address", s.company_postal_address) or ""
    cfg.privacy_url = pick("privacy_url", s.privacy_policy_url) or ""
    cfg.portfolio_url = s.portfolio_url if admin else ""
    cfg.smtp_host = pick("smtp_host", s.smtp_host) or ""
    cfg.smtp_port = int(pick("smtp_port", s.smtp_port) or 465)
    ssl = pick("smtp_use_ssl", s.smtp_use_ssl)
    cfg.smtp_use_ssl = bool(ssl) if ssl is not None else cfg.smtp_port == 465
    cfg.smtp_user = pick("smtp_user", s.smtp_user) or ""
    cfg.smtp_pass = decrypt(row.smtp_pass_enc) if row and row.smtp_pass_enc else (s.smtp_pass if admin else "")
    cfg.imap_host = pick("imap_host", s.imap_host) or ""
    cfg.imap_port = int(pick("imap_port", s.imap_port) or 993)
    cfg.imap_user = pick("imap_user", s.imap_user) or ""
    cfg.imap_pass = decrypt(row.imap_pass_enc) if row and row.imap_pass_enc else (s.imap_pass if admin else "")
    cfg.imap_folder = pick("imap_folder", s.imap_folder) or "INBOX"
    cfg.daily_cap = int(pick("daily_cap", s.daily_email_cap_global) or CUSTOMER_DEFAULT_DAILY_CAP)
    cfg.timezone = pick("timezone", s.scheduler_timezone) or s.scheduler_timezone
    return cfg
