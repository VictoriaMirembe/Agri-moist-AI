# Shared test fixtures. Every test gets its own temp SQLite file, never the real
# data/agrimoist.db, and DATABASE_URL is set as a real environment variable (not just
# a Python object mutation) so that agent runs - which call our tools through a
# separate MCP server subprocess - see the same temp database too.
from __future__ import annotations

from backend.db.seed import DEMO_OFFICER_PASSWORD
import pytest
from sqlmodel import create_engine


@pytest.fixture(autouse=True)
def no_model_key(monkeypatch):
    """Force the deterministic rule-based (stub) mode in every test, so no test ever
    calls the real Groq model (or Sunbird) even when .env has the keys. The env var covers
    subprocesses and any fresh Settings(); the attribute covers the already-loaded
    settings object."""
    from backend.agent import model_client
    from backend.core.config import settings

    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setattr(settings, "groq_api_key", None)
    monkeypatch.setattr(model_client, "_client", None)
    monkeypatch.setenv("SUNBIRD_API_KEY", "")  # nor the real Sunbird translator
    monkeypatch.setattr(settings, "sunbird_api_key", None)
    # Tests run on the made-up seed data and stored sensor readings only, never on
    # live soil data, and never on the real (Neon) database.
    monkeypatch.setenv("SEED_DEMO_DATA", "true")
    monkeypatch.setattr(settings, "seed_demo_data", True)
    monkeypatch.setenv("SOIL_SOURCE", "sensor-only")
    monkeypatch.setattr(settings, "soil_source", "sensor-only")
    monkeypatch.setattr(settings, "keep_tool_servers_warm", False)
    monkeypatch.setattr(settings, "translate_in_background", False)


@pytest.fixture()
def fake_translator(monkeypatch):
    """Pretend a translator is configured and answers with the given result."""
    from backend.approvals import translation_jobs

    def use(result: dict) -> None:
        monkeypatch.setattr(translation_jobs, "translator_available", lambda: True)
        monkeypatch.setattr(translation_jobs, "translate_text", lambda text, lang: dict(result))

    return use
    monkeypatch.setattr(settings, "soil_source", "sensor-only")


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
    (test_db already pointed DATABASE_URL and the engine at it), signed in as officer 1."""
    from fastapi.testclient import TestClient

    from backend.main import app

    from backend.core.config import settings

    with TestClient(app) as c:
        r = c.post("/api/auth/login", json={"officer_id": 1, "password": DEMO_OFFICER_PASSWORD})
        assert r.status_code == 200, r.text
        yield c


@pytest.fixture()
def unauthenticated_client(test_db):
    """A TestClient with no session or cookie set, to test role protection."""
    from fastapi.testclient import TestClient

    from backend.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def farmer_client(test_db):
    """sign_in(phone) -> a TestClient signed in as that seeded farmer (PIN sign-in),
    separate from the officer `client`."""
    from fastapi.testclient import TestClient

    from backend.db.seed import DEMO_FARMER_PIN
    from backend.main import app

    clients = []

    def sign_in(phone: str) -> TestClient:
        c = TestClient(app)
        c.__enter__()
        clients.append(c)
        r = c.post("/farmer/login", data={"phone": phone, "pin": DEMO_FARMER_PIN}, follow_redirects=False)
        assert r.status_code == 303, r.text
        return c

    yield sign_in
    for c in clients:
        c.__exit__(None, None, None)
