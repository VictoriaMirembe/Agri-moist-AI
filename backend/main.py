# AgriMoist AI - FastAPI app entry point. Creates the database and loads the demo
# seed data on first start, then mounts the officer, approvals and farmer routers,
# the officer/farmer HTML screens, and the USSD simulator page.
from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend.approvals.router import router as approvals_router
from backend.core.config import settings
from backend.core.database import init_db, session_scope
from backend.db.seed import seed
from backend.farmer.pages import router as farmer_pages_router
from backend.farmer.router import router as farmer_router
from backend.officer.pages import api_router as officer_pages_api_router
from backend.officer.pages import public_router as officer_login_router
from backend.officer.pages import router as officer_pages_router
from backend.officer.router import auth_router as officer_auth_router
from backend.officer.router import router as officer_router

REPO_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as session:
        seed(session)
    yield


app = FastAPI(
    title="AgriMoist AI",
    description="A field assistant for agricultural extension officers in Karamoja, Uganda.",
    lifespan=lifespan,
)

@app.middleware("http")
async def revalidate_static_files(request: Request, call_next):
    """Make browsers check for a newer styles.css / app.js on every page load (a cheap
    304 when unchanged), so a redeploy never leaves anyone on stale styles."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# Server-side signed session cookies for extension officer authentication (runs outermost).
# This signed session is the only thing that makes a request "signed in"; plain role
# cookies or headers are not trusted (see backend/officer/auth.py).
app.add_middleware(
    SessionMiddleware,
    # From SESSION_SECRET in .env. If unset, a random key per start: officers simply
    # sign in again after a restart, and no key ever lives in the repository.
    secret_key=settings.session_secret or secrets.token_urlsafe(32),
    session_cookie="agrimoist_session",
    max_age=86400 * 7,
    same_site="lax",
    https_only=False,
)


app.include_router(officer_auth_router)
app.include_router(officer_router)
app.include_router(approvals_router)
app.include_router(officer_login_router)
app.include_router(farmer_router)
app.include_router(officer_pages_router)
app.include_router(officer_pages_api_router)
app.include_router(farmer_pages_router)

static_dir = REPO_ROOT / "frontend" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}
