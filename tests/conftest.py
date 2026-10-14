# Shared test fixtures. Every test gets its own temp SQLite file, never the real
# data/agrimoist.db, and DATABASE_URL is set as a real environment variable (not just
# a Python object mutation) so that agent runs - which call our tools through a
# separate MCP server subprocess - see the same temp database too.
from __future__ import annotations

import pytest
from sqlmodel import create_engine


@pytest.fixture(autouse=True)
def no_model_key(monkeypatch):
    """Force the deterministic rule-based (stub) mode in every test, so no test ever
    calls the real Groq model even when .env has GROQ_API_KEY. The env var covers
    subprocesses and any fresh Settings(); the attribute covers the already-loaded
    settings object."""
    from backend.agent import model_client
    from backend.core.config import settings

    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(model_client, "_client", None)


@pytest.fixture()
def test_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    db_url = f"sqlite:///{db_path}"
    monkeypatch.setenv("DATABASE_URL", db_url)

    import backend.core.database as database_module

    database_module.engine = create_engine(db_url, echo=False, connect_args={"check_same_thread": False})
    yield database_module
    database_module.engine.dispose()


@pytest.fixture()
def seeded_session(test_db):
    """A session on a fresh, seeded temp database - for testing tool functions and
    the agent directly, without going through the HTTP layer."""
    from backend.db.seed import seed

    test_db.init_db()
    with test_db.session_scope() as session:
        seed(session)
    with test_db.session_scope() as session:
        yield session


@pytest.fixture()
def client(test_db):
    """A TestClient whose app lifespan creates and seeds the same temp database
    (test_db already pointed DATABASE_URL and the engine at it)."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app, cookies={"officer_role": "officer"}) as c:
        yield c


@pytest.fixture()
def unauthenticated_client(test_db):
    """A TestClient with no session or cookie set, to test role protection."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        yield c
