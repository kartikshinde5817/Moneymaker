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
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.db import get_db
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])

# Paths reachable with no session. Keep this list short and deliberate.
PUBLIC_PREFIXES = ("/auth/", "/u/", "/static/", "/favicon.ico")


def _set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        auth.COOKIE_NAME, token,
        max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="lax",
        secure=settings.public_base_url.startswith("https://"),
    )


class LoginIn(BaseModel):
    password: str


@router.post("/login")
def login(payload: LoginIn, response: Response, db: Session = Depends(get_db)):
    if not auth.login_required():
        return {"ok": True, "needs_totp": False}

    user = auth.check_password(db, payload.password)
    if user is None:
        raise HTTPException(401, "Incorrect password")

    if user.totp_confirmed:
        # Second factor still required; no cookie until it is entered.
        return {"ok": True, "needs_totp": True, "needs_setup": False}

    # No 2FA yet: let them in, but push them straight to enrollment. A
    # half-finished account must never become a way to skip the second
    # factor permanently, so /auth/me keeps reporting `totp_required: true`
    # until it is confirmed.
    token = auth.create_session(db, user)
    db.commit()
    _set_cookie(response, token)
    auth.ensure_totp_secret(db, user)
    db.commit()
    return {"ok": True, "needs_totp": False, "needs_setup": True}


class TotpIn(BaseModel):
    password: str
    code: str


@router.post("/totp/verify")
def totp_verify(payload: TotpIn, response: Response, db: Session = Depends(get_db)):
    """Second factor at login time (password already confirmed, pre-session)."""
    user = auth.check_password(db, payload.password)
    if user is None:
        raise HTTPException(401, "Incorrect password")
    if not auth.verify_totp(user, payload.code):
        raise HTTPException(401, "Incorrect code")
    token = auth.create_session(db, user)
    db.commit()
    _set_cookie(response, token)
    return {"ok": True}


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
        return {"authenticated": True, "login_required": False, "totp_required": False}
    user = auth.current_user(request, db)
    return {
        "authenticated": user is not None,
        "login_required": True,
        "totp_required": bool(user and not user.totp_confirmed),
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


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Money Maker — sign in</title>
<style>
:root{--ink:#152029;--bg:#EEF2F5;--panel:#FFF;--line:#DCE4EA;--muted:#5D6D7B;--brand:#0E7490;--brand-2:#06B6D4;--warn:#DC2626}
*{box-sizing:border-box}
body{font-family:Inter,system-ui,sans-serif;background:var(--bg);color:var(--ink);display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:32px 30px;width:360px;max-width:92vw;box-shadow:0 20px 50px -20px rgba(0,0,0,.15)}
h1{font-size:19px;margin:0 0 4px}
p.sub{color:var(--muted);font-size:13px;margin:0 0 22px}
label{font-size:12.5px;font-weight:600;display:block;margin-bottom:6px}
input{width:100%;border:1px solid var(--line);border-radius:8px;padding:11px 12px;font-size:15px;background:#F6F9FB;margin-bottom:14px}
input:focus{outline:2px solid var(--brand-2);background:#fff}
button{width:100%;background:var(--brand);color:#fff;border:0;border-radius:8px;padding:12px;font-size:14px;font-weight:600;cursor:pointer}
button:hover{filter:brightness(1.08)}
.err{color:var(--warn);font-size:13px;margin:-6px 0 14px;display:none}
.qr{text-align:center;margin:6px 0 16px}
.qr img{border:1px solid var(--line);border-radius:8px}
.secret{font-family:ui-monospace,Menlo,monospace;font-size:12px;background:#F6F9FB;border:1px solid var(--line);border-radius:6px;padding:8px;word-break:break-all;margin-bottom:14px;text-align:center}
.step{display:none}
.step.on{display:block}
.hint{font-size:12px;color:var(--muted);margin:0 0 14px;line-height:1.5}
</style></head>
<body>
<div class="card">
  <div id="stepPassword" class="step on">
    <h1>Money Maker</h1>
    <p class="sub">Sign in to the dashboard</p>
    <label for="pw">Password</label>
    <input id="pw" type="password" autocomplete="current-password" autofocus>
    <div class="err" id="errPw"></div>
    <button id="goPw">Continue</button>
  </div>

  <div id="stepTotp" class="step">
    <h1>Verification code</h1>
    <p class="sub">Enter the 6-digit code from Google Authenticator</p>
    <label for="code">Code</label>
    <input id="code" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code">
    <div class="err" id="errCode"></div>
    <button id="goCode">Verify</button>
  </div>

  <div id="stepSetup" class="step">
    <h1>Set up Google Authenticator</h1>
    <p class="hint">Scan this with the Google Authenticator app (or Authy /
      1Password), then enter the 6-digit code it shows to confirm.</p>
    <div class="qr"><img id="qrImg" width="200" height="200"></div>
    <div class="secret" id="qrSecret"></div>
    <label for="confirmCode">Code from the app</label>
    <input id="confirmCode" inputmode="numeric" pattern="[0-9]*" maxlength="6">
    <div class="err" id="errConfirm"></div>
    <button id="goConfirm">Confirm &amp; finish</button>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
let PW='';
function show(id){document.querySelectorAll('.step').forEach(s=>s.classList.remove('on'));$('#'+id).classList.add('on');}
function err(id,msg){const e=$(id);e.textContent=msg;e.style.display=msg?'block':'none';}

$('#goPw').onclick=async()=>{
  PW=$('#pw').value;
  err('#errPw','');
  const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:PW})});
  const d=await r.json();
  if(!r.ok){err('#errPw',d.detail||'Incorrect password');return;}
  if(d.needs_totp){show('stepTotp');$('#code').focus();}
  else if(d.needs_setup){await loadQr();show('stepSetup');}
  else location.href='/';
};
$('#pw').addEventListener('keydown',e=>{if(e.key==='Enter')$('#goPw').click();});

$('#goCode').onclick=async()=>{
  err('#errCode','');
  const r=await fetch('/auth/totp/verify',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:PW,code:$('#code').value})});
  if(!r.ok){const d=await r.json();err('#errCode',d.detail||'Incorrect code');return;}
  location.href='/';
};
$('#code').addEventListener('keydown',e=>{if(e.key==='Enter')$('#goCode').click();});

async function loadQr(){
  const r=await fetch('/auth/totp/qr');
  const secret=r.headers.get('X-TOTP-Secret')||'';
  $('#qrSecret').textContent=secret?('Can\\'t scan? Enter manually: '+secret):'';
  const blob=await r.blob();
  $('#qrImg').src=URL.createObjectURL(blob);
}

$('#goConfirm').onclick=async()=>{
  err('#errConfirm','');
  const r=await fetch('/auth/totp/confirm',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:$('#confirmCode').value})});
  if(!r.ok){const d=await r.json();err('#errConfirm',d.detail||'Incorrect code');return;}
  location.href='/';
};
$('#confirmCode').addEventListener('keydown',e=>{if(e.key==='Enter')$('#goConfirm').click();});
</script>
</body></html>"""


@router.get("/login-page", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse(LOGIN_PAGE)


class SessionGuardMiddleware(BaseHTTPMiddleware):
    """Redirect/401 anything outside PUBLIC_PREFIXES with no valid session."""

    async def dispatch(self, request: Request, call_next):
        if not auth.login_required():
            return await call_next(request)

        path = request.url.path
        if path == "/" or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            if path == "/":
                from app.db import SessionLocal
                db = SessionLocal()
                try:
                    user = auth.current_user(request, db)
                finally:
                    db.close()
                if user is None:
                    from fastapi.responses import RedirectResponse
                    return RedirectResponse("/auth/login-page")
            return await call_next(request)

        from app.db import SessionLocal
        db = SessionLocal()
        try:
            user = auth.current_user(request, db)
        finally:
            db.close()
        if user is None:
            if path.startswith("/api/"):
                from fastapi.responses import JSONResponse
                return JSONResponse({"detail": "Not authenticated"}, status_code=401)
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/auth/login-page")
        return await call_next(request)
