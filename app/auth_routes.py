"""Login page, 2FA enrollment, and the middleware that guards everything else.

Flow:
  1. POST /auth/login with the password.
     - No TOTP confirmed yet -> logged in immediately, `setup_totp: true` in
       the response so the frontend shows the QR code next.
     - TOTP confirmed -> response asks for the code; no session yet.
  2. POST /auth/totp/verify with the 6-digit code -> session cookie set.
  3. GET /auth/totp/qr -> the QR code image, only reachable once the password
     step has already happened in this request's session attempt.

Everything under /api, the dashboard page, and /static is behind the
middleware. /auth/*, the public unsubscribe link, and health checks are not.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.db import get_db
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])

# Paths reachable with no session. Keep this list short and deliberate.
PUBLIC_PREFIXES = ("/auth/", "/u/", "/static/", "/favicon.ico", "/api/public/",
                   "/billing/razorpay/webhook")
PUBLIC_PAGES = ("/", "/login", "/signup", "/pricing")


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
        secure=settings.public_base_url.startswith("https://"),
    )


class LoginIn(BaseModel):
    email: str = ""
    password: str


def _start_session(db: Session, response: Response, user) -> None:
    from app.models import utcnow

    user.last_login_at = utcnow()
    token = auth.create_session(db, user)
    db.commit()
    _set_cookie(response, token)


@router.post("/login")
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    if not auth.login_required():
        return {"ok": True, "needs_totp": False, "role": "admin"}

    email = payload.email.strip().lower()
    user = auth.authenticate(db, email, payload.password) if email else auth.check_password(db, payload.password)
    if user is None:
        raise HTTPException(401, "Incorrect email or password")
    if user.status != "active":
        raise HTTPException(403, "This account has been blocked. Please contact support.")

    if user.role == "admin":
        if user.totp_confirmed:
            # Second factor still required; no cookie until it is entered.
            return {"ok": True, "needs_totp": True, "needs_setup": False, "role": "admin"}
        # No 2FA yet: let them in, but push them straight to enrollment.
        _start_session(db, response, user)
        auth.ensure_totp_secret(db, user)
        db.commit()
        return {"ok": True, "needs_totp": False, "needs_setup": True, "role": "admin"}

    _start_session(db, response, user)
    return {"ok": True, "needs_totp": False, "needs_setup": False, "role": user.role}


class SignupIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    email: str = Field(min_length=5, max_length=255)
    password: str = Field(min_length=8, max_length=200)
    company: str = Field("", max_length=160)


@router.post("/signup")
def signup(payload: SignupIn, response: Response, db: Session = Depends(get_db)):
    from email_validator import EmailNotValidError, validate_email
    from sqlalchemy import select

    from app.models import User

    try:
        email = validate_email(payload.email.strip(), check_deliverability=False).normalized.lower()
    except EmailNotValidError:
        raise HTTPException(400, "Please enter a valid email address.") from None
    if email == (settings.admin_email or "").lower() or db.execute(
            select(User).where(User.email == email)).scalar_one_or_none():
        raise HTTPException(409, "An account with this email already exists. Please sign in.")
    user = auth.create_customer(db, email=email, password=payload.password,
                                name=payload.name, company=payload.company)
    _start_session(db, response, user)
    return {"ok": True, "role": "customer"}


class TotpIn(BaseModel):
    email: str = ""
    password: str
    code: str


@router.post("/totp/verify")
def totp_verify(payload: TotpIn, response: Response, db: Session = Depends(get_db)):
    """Second factor at login time (password already confirmed, pre-session)."""
    email = payload.email.strip().lower()
    user = auth.authenticate(db, email, payload.password) if email else auth.check_password(db, payload.password)
    if user is None or user.role != "admin":
        raise HTTPException(401, "Incorrect email or password")
    if not auth.verify_totp(user, payload.code):
        raise HTTPException(401, "Incorrect code")
    _start_session(db, response, user)
    return {"ok": True, "role": "admin"}


@router.get("/totp/qr")
def totp_qr(request: Request, db: Session = Depends(get_db)):
    """The QR code for enrollment. Only for an already-logged-in, unconfirmed user."""
    user = auth.current_user(request, db)
    if user is None:
        raise HTTPException(401, "Not logged in")
    if user.totp_confirmed:
        raise HTTPException(400, "2FA is already set up — disable it first to re-enroll")
    secret = auth.ensure_totp_secret(db, user)
    db.commit()
    png = auth.provisioning_qr_png(user)
    from fastapi.responses import Response as RawResponse
    return RawResponse(
        content=png, media_type="image/png",
        headers={"X-TOTP-Secret": secret},  # manual-entry fallback
    )


class ConfirmIn(BaseModel):
    code: str


@router.post("/totp/confirm")
def totp_confirm(payload: ConfirmIn, request: Request, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    if user is None:
        raise HTTPException(401, "Not logged in")
    if not auth.confirm_totp(db, user, payload.code):
        raise HTTPException(401, "Incorrect code — check the time on your phone and try again")
    db.commit()
    return {"ok": True}


@router.post("/totp/disable")
def totp_disable(request: Request, db: Session = Depends(get_db)):
    """Turn 2FA off. Requires an active session, i.e. you must be logged in."""
    user = auth.current_user(request, db)
    if user is None:
        raise HTTPException(401, "Not logged in")
    auth.disable_totp(db, user)
    db.commit()
    return {"ok": True}


@router.get("/me")
def me(request: Request, db: Session = Depends(get_db)):
    if not auth.login_required():
        return {"authenticated": True, "login_required": False, "totp_required": False, "role": "admin"}
    user = auth.current_user(request, db)
    return {
        "authenticated": user is not None,
        "login_required": True,
        "role": user.role if user else None,
        "name": user.name if user else None,
        "totp_required": bool(user and user.role == "admin" and not user.totp_confirmed),
    }


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    auth.destroy_session(db, request.cookies.get(auth.COOKIE_NAME))
    db.commit()
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@router.get("/logout-everywhere")
def logout_everywhere(request: Request, response: Response, db: Session = Depends(get_db)):
    user = auth.current_user(request, db)
    if user:
        auth.destroy_all_sessions(db, user.id)
        db.commit()
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@router.get("/login-page", include_in_schema=False)
def login_page():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/login")


class SessionGuardMiddleware(BaseHTTPMiddleware):
    """Who is asking, and are they allowed here.

    Public pages and the paths in PUBLIC_PREFIXES need no session. Everything
    else does: pages redirect to the sign-in page, the API answers 401. The
    signed-in identity is put on request.state.user for the API to scope by.
    """

    async def dispatch(self, request: Request, call_next):
        if not auth.login_required():
            return await call_next(request)

        from fastapi.responses import JSONResponse, RedirectResponse

        from app.db import SessionLocal

        path = request.url.path
        token = request.cookies.get(auth.COOKIE_NAME)
        # The database call runs in a worker thread: done inline it blocked the
        # event loop, so every other request waited on this one's round trip.
        ident = await run_in_threadpool(auth.session_identity, SessionLocal, token) if token else None
        request.state.user = ident

        if path in PUBLIC_PAGES or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)
        if ident is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            return RedirectResponse("/login")
        if ident.get("status") != "active":
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Your account has been blocked."}, status_code=403)
            return RedirectResponse("/login?suspended=1")
        if path.startswith("/api/admin") and ident.get("role") != "admin":
            return JSONResponse({"detail": "Only the platform admin can do this"}, status_code=403)
        return await call_next(request)
