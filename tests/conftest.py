# Shared test fixtures. Every test gets its own temp SQLite file, never the real
# data/agrimoist.db, and DATABASE_URL is set as a real environment variable (not just
# a Python object mutation) so that agent runs - which call our tools through a
# separate MCP server subprocess - see the same temp database too.
from __future__ import annotations

import pytest
from sqlmodel import create_engine


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

    with TestClient(app) as c:
        yield c
