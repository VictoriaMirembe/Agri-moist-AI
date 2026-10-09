# AgriMoist AI - FastAPI app entry point. Creates the database and loads the demo
# seed data on first start, then mounts the officer, approvals and farmer routers,
# the officer/farmer HTML screens, and the USSD simulator page.
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.approvals.router import router as approvals_router
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
