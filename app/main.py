"""Application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.api import public_router, router
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
    from app.seed import bootstrap, seed_mailbox

    with session_scope() as db:
        result = bootstrap(db)
        seed_mailbox(db)
        from app.services.pipeline import close_dead_runs
        closed = close_dead_runs(db, "Interrupted - the app restarted while this run was in progress.")
    if closed:
        log.info("closed %d run(s) interrupted by the restart", closed)
    if result["campaigns_created"]:
        log.info("created %d default campaigns", result["campaigns_created"])
    if result["legacy_leads_migrated"]:
        log.info("migrated %d leads from the previous version", result["legacy_leads_migrated"])

    sched.start()
    yield
    sched.shutdown()


app = FastAPI(title="Money Maker", lifespan=lifespan)
app.add_middleware(SessionGuardMiddleware)
app.add_middleware(GZipMiddleware, minimum_size=1000)
app.include_router(router)
app.include_router(public_router)
app.include_router(auth_router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/api/scheduler")
def scheduler_state():
    from app import scheduler as sched

    return {"enabled": settings.scheduler_enabled, "jobs": sched.jobs()}


@app.get("/", response_class=HTMLResponse)
def index():
    page = STATIC_DIR / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>Money Maker</h1><p>Dashboard assets are missing.</p>", 500)
    return HTMLResponse(page.read_text(encoding="utf-8"))
