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
import threading
import time

import pyotp
import qrcode
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Request
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DBSession

from app.config import settings
from app.models import AuthToken
from app.models import Session as SessionRow
from app.models import User, utcnow

COOKIE_NAME = "mm_session"
SESSION_DAYS = 14
ADMIN_EMAIL = "admin@local"

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, Exception):  # noqa: BLE001 - malformed hash = no match
        return False


def admin_user(db: DBSession) -> User:
    """The platform owner. ADMIN_EMAIL / ADMIN_PASSWORD in the environment are
    the source of truth: editing them changes the admin login on next use."""
    user = db.execute(
        select(User).where(User.role == "admin").order_by(User.id)
    ).scalars().first()
    if user is None:
        from app.db import ensure_admin_row
        ensure_admin_row(db.connection())
        user = db.execute(select(User).where(User.role == "admin").order_by(User.id)).scalars().first()
    email = (settings.admin_email or "").strip().lower()
    if email and user.email != email:
        taken = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if taken is None:
            user.email = email
    if settings.admin_password and not verify_password(user.password_hash, settings.admin_password):
        user.password_hash = hash_password(settings.admin_password)
    db.flush()
    return user


# Kept for older call sites.
_get_or_create_admin = admin_user


def login_required() -> bool:
    return bool(settings.admin_password)


def authenticate(db: DBSession, email: str, password: str) -> User | None:
    email = (email or "").strip().lower()
    admin = admin_user(db)
    user = admin if email == admin.email else db.execute(
        select(User).where(User.email == email)).scalar_one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        return None
    return user


def check_password(db: DBSession, password: str) -> User | None:
    """Admin-only password check (the original single-user login)."""
    user = admin_user(db)
    return user if verify_password(user.password_hash, password) else None


def create_customer(db: DBSession, *, email: str, password: str, name: str,
                    company: str = "") -> User:
    user = User(email=email.strip().lower(), password_hash=hash_password(password),
                role="customer", name=name.strip()[:160], company=(company or "").strip()[:160] or None,
                status="active")
    db.add(user)
    db.flush()
    return user


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


# The session guard runs on every request. With the database a long network
# hop away, two queries per request made every page slow, so a token that was
# valid a moment ago is trusted for SESSION_CACHE_SECONDS. Logging out clears
# the cache, so a logout still takes effect at once.
SESSION_CACHE_SECONDS = 60
_session_cache: dict[str, tuple[float, dict]] = {}
_session_cache_lock = threading.Lock()


def session_identity(db_factory, token: str | None) -> dict | None:
    """{"id", "role", "status"} for a live session, else None.

    Blocking: run off the event loop.
    """
    if not token:
        return None
    now = time.monotonic()
    with _session_cache_lock:
        hit = _session_cache.get(token)
        if hit and hit[0] > now:
            return hit[1]
    db = db_factory()
    try:
        user = resolve_session(db, token)
        ident = {"id": user.id, "role": user.role, "status": user.status} if user else None
        db.commit()
    finally:
        db.close()
    if ident:
        with _session_cache_lock:
            _session_cache[token] = (now + SESSION_CACHE_SECONDS, ident)
    return ident


def session_is_valid(db_factory, token: str | None) -> bool:
    return session_identity(db_factory, token) is not None


def _forget_sessions(token: str | None = None) -> None:
    with _session_cache_lock:
        if token is None:
            _session_cache.clear()
        else:
            _session_cache.pop(token, None)


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
    _forget_sessions(token)
    db.execute(delete(SessionRow).where(SessionRow.token == token))
    db.flush()


def destroy_all_sessions(db: DBSession, user_id: int) -> None:
    _forget_sessions()
    db.execute(delete(SessionRow).where(SessionRow.user_id == user_id))
    db.flush()


def current_user(request: Request, db: DBSession) -> User | None:
    return resolve_session(db, request.cookies.get(COOKIE_NAME))


# --- emailed one-time links: verify an address, reset a password --------------

import hashlib

TOKEN_LIFETIME = {"verify": dt.timedelta(hours=48), "reset": dt.timedelta(hours=1)}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token(db: DBSession, user: User, kind: str) -> str:
    """A fresh link token. Any earlier unused one of the same kind stops working."""
    db.execute(delete(AuthToken).where(AuthToken.user_id == user.id, AuthToken.kind == kind,
                                       AuthToken.used_at.is_(None)))
    token = secrets.token_urlsafe(32)
    db.add(AuthToken(user_id=user.id, kind=kind, token_hash=_hash_token(token),
                     expires_at=utcnow() + TOKEN_LIFETIME[kind]))
    db.flush()
    return token


def redeem_token(db: DBSession, token: str, kind: str) -> User | None:
    """Use up a token and return its user, or None if it is unknown, used or expired."""
    from app.models import as_aware

    row = db.execute(select(AuthToken).where(
        AuthToken.token_hash == _hash_token(token or ""), AuthToken.kind == kind)).scalar_one_or_none()
    if row is None or row.used_at is not None or as_aware(row.expires_at) < utcnow():
        return None
    row.used_at = utcnow()
    db.flush()
    return db.get(User, row.user_id)
