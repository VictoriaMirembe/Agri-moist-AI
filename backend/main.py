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
from backend.officer.pages import router as officer_pages_router
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
async def sync_auth_session_middleware(request: Request, call_next):
    """Ensure cookie or header role is synchronized into request.session."""
    if "session" in request.scope:
        role_cookie = request.cookies.get("officer_role") or request.cookies.get("role")
        if role_cookie and "role" not in request.session:
            request.session["role"] = role_cookie
        role_header = request.headers.get("x-officer-role") or request.headers.get("x-role")
        if role_header and "role" not in request.session:
            request.session["role"] = role_header
    return await call_next(request)


# Server-side signed session cookies for extension officer authentication (runs outermost)
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


app.include_router(officer_router)
app.include_router(approvals_router)
app.include_router(farmer_router)
app.include_router(officer_pages_router)
app.include_router(farmer_pages_router)

static_dir = REPO_ROOT / "frontend" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}
