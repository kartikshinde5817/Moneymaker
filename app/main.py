"""Application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import RedirectResponse
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.admin_api import router as admin_router
from app.api import admin_only, public_router, router
from app.auth_routes import SessionGuardMiddleware
from app.auth_routes import router as auth_router
from app.config import settings
from app.db import init_db, session_scope

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("moneymaker")

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    from app import scheduler as sched
    from app.seed import bootstrap

    with session_scope() as db:
        result = bootstrap(db)
        from app.services.pipeline import close_dead_runs
        closed = close_dead_runs(db, "Interrupted - the app restarted while this run was in progress.")
    if closed:
        log.info("closed %d run(s) interrupted by the restart", closed)

    sched.start()
    yield
    sched.shutdown()


app = FastAPI(title="Money Maker", lifespan=lifespan)
app.add_middleware(SessionGuardMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(router)
app.include_router(admin_router)
app.include_router(public_router)
app.include_router(auth_router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/scheduler", dependencies=[Depends(admin_only)])
def scheduler_state():
    from app import scheduler as sched

    return {"enabled": settings.scheduler_enabled, "jobs": sched.jobs()}


def _page(name: str) -> HTMLResponse:
    page = STATIC_DIR / name
    if not page.exists():
        return HTMLResponse(f"<h1>{settings.platform_name}</h1><p>Page assets are missing.</p>", 500)
    html = page.read_text(encoding="utf-8").replace("{{PLATFORM}}", settings.platform_name)
    for asset in ("app.js", "product.js"):  # version the script URLs so a deploy never serves stale JS
        f = STATIC_DIR / asset
        if f.exists():
            html = html.replace(f'/static/{asset}"', f'/static/{asset}?v={int(f.stat().st_mtime)}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    return _page("landing.html")


@app.get("/pricing", include_in_schema=False)
def pricing():
    return RedirectResponse("/#pricing")


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
@app.get("/signup", response_class=HTMLResponse, include_in_schema=False)
def auth_page():
    return _page("auth.html")


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return _page("index.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return RedirectResponse("/app#admin")
