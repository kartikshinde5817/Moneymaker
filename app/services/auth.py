"""Login: password + TOTP (Google Authenticator), session cookies.

Two factors, both required once set up:
  1. the password in ADMIN_PASSWORD
  2. a 6-digit code from Google Authenticator (or Authy, 1Password, etc.)

First login after a password-only account exists shows a QR code. Scanning it
and entering one correct code confirms the secret and turns on the second
factor for every login after that - the secret is stored but inert until
confirmed, so a half-finished setup can't lock the owner out.

Sessions are opaque random tokens in an HTTP-only cookie, looked up against a
server-side table. Not JWT: a session needs to be revocable by deleting one
row (the "log out everywhere" case matters more here than statelessness).
"""

from __future__ import annotations

import datetime as dt
import io
import secrets

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models import Session as SessionRow
from app.models import User, utcnow

COOKIE_NAME = "mm_session"
SESSION_DAYS = 14
ADMIN_EMAIL = "admin@local"

_hasher = PasswordHasher()


def _get_or_create_admin(db: DBSession) -> User:
    """The single admin account, created from ADMIN_PASSWORD on first use.

    If ADMIN_PASSWORD changes in .env, the stored hash is refreshed to match -
    the environment variable is the source of truth, not the database.
    """
    user = db.execute(select(User).where(User.email == ADMIN_EMAIL)).scalar_one_or_none()
    if user is None:
        user = User(email=ADMIN_EMAIL, password_hash=_hasher.hash(settings.admin_password))
        db.add(user)
        db.flush()
        return user
    try:
        _hasher.verify(user.password_hash, settings.admin_password)
    except VerifyMismatchError:
        # .env was edited since the hash was stored - follow it.
        user.password_hash = _hasher.hash(settings.admin_password)
        db.flush()
    return user


def login_required() -> bool:
    return bool(settings.admin_password)


def check_password(db: DBSession, password: str) -> User | None:
    user = _get_or_create_admin(db)
    try:
        _hasher.verify(user.password_hash, password)
        return user
    except VerifyMismatchError:
        return None


# --- TOTP (Google Authenticator) -------------------------------------------


def ensure_totp_secret(db: DBSession, user: User) -> str:
    """A secret always exists so the QR code can be shown before confirmation."""
    if not user.totp_secret:
        user.totp_secret = pyotp.random_base32()
        db.flush()
    return user.totp_secret


def provisioning_qr_png(user: User, issuer: str = "Money Maker") -> bytes:
    uri = pyotp.TOTP(user.totp_secret).provisioning_uri(
        name=user.email, issuer_name=issuer
    )
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def verify_totp(user: User, code: str) -> bool:
    if not user.totp_secret:
        return False
    # valid_window=1 tolerates the clock drift that makes "the code just
    # changed as I typed it" the single most common 2FA support complaint.
    return pyotp.TOTP(user.totp_secret).verify((code or "").strip(), valid_window=1)


def confirm_totp(db: DBSession, user: User, code: str) -> bool:
    if verify_totp(user, code):
        user.totp_confirmed = True
        db.flush()
        return True
    return False


def disable_totp(db: DBSession, user: User) -> None:
    user.totp_secret = None
    user.totp_confirmed = False
    db.flush()


# --- sessions ----------------------------------------------------------------


def create_session(db: DBSession, user: User) -> str:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    db.add(SessionRow(
        token=token, user_id=user.id, created_at=now,
        expires_at=now + dt.timedelta(days=SESSION_DAYS), last_seen_at=now,
    ))
    db.flush()
    return token


def resolve_session(db: DBSession, token: str | None) -> User | None:
    if not token:
        return None
    row = db.execute(
        select(SessionRow).where(SessionRow.token == token)
    ).scalar_one_or_none()
    if row is None:
        return None
    expires = row.expires_at
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if expires < utcnow():
        db.delete(row)
        db.flush()
        return None
    row.last_seen_at = utcnow()
    return db.get(User, row.user_id)


def destroy_session(db: DBSession, token: str | None) -> None:
    if not token:
        return
    db.execute(delete(SessionRow).where(SessionRow.token == token))
    db.flush()


def destroy_all_sessions(db: DBSession, user_id: int) -> None:
    db.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
    db.flush()


def current_user(request: Request, db: DBSession) -> User | None:
    return resolve_session(db, request.cookies.get(COOKIE_NAME))
