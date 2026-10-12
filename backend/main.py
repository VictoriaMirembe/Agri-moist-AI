# AgriMoist AI - FastAPI app entry point. Creates the database and loads the demo
# seed data on first start, then mounts the officer, approvals and farmer routers,
# the officer/farmer HTML screens, and the USSD simulator page.
from __future__ import annotations

from contextlib import asynccontextmanager
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend.approvals.router import router as approvals_router
from backend.auth_router import provision_officer_login, router as auth_router
from backend.core.config import settings
from backend.core.database import init_db, session_scope
from backend.db.seed import seed
from backend.farmer.pages import router as farmer_pages_router
from backend.farmer.router import router as farmer_router
from backend.officer.auth_pages import router as officer_auth_pages_router
from backend.officer.pages import router as officer_pages_router
from backend.officer.router import router as officer_router
from backend.ussd_router import router as ussd_router

REPO_ROOT = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as session:
        seed(session)
        provision_officer_login(session)
    yield


app = FastAPI(
    title="AgriMoist AI",
    description="A field assistant for agricultural extension officers in Karamoja, Uganda.",
    lifespan=lifespan,
)

if not settings.session_secret_key and settings.app_env.lower() == "production":
    raise RuntimeError("SESSION_SECRET_KEY must be set when APP_ENV=production.")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret_key or secrets.token_urlsafe(32),
    max_age=8 * 60 * 60,
    same_site="lax",
    https_only=settings.app_env.lower() == "production",
)


@app.get("/", include_in_schema=False)
def landing_page(request: Request):
    if request.session.get("role") == "officer":
        return RedirectResponse("/officer/dashboard", status_code=303)
    return RedirectResponse("/farmer", status_code=303)


app.include_router(officer_router)
app.include_router(approvals_router)
app.include_router(auth_router)
app.include_router(farmer_router)
app.include_router(ussd_router)
app.include_router(officer_pages_router)
app.include_router(farmer_pages_router)
app.include_router(officer_auth_pages_router)

static_dir = REPO_ROOT / "frontend" / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/health")
def health():
    return {"status": "ok"}
