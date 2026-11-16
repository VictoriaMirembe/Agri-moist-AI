# Real data, not demo data: first-time setup, officers registering villages and
# farmers, farmer PIN sign-in on real registrations, Open-Meteo modelled soil moisture,
# and the plain "Do not plant" instruction.
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def empty_app(test_db, monkeypatch):
    """The app as it starts for real: no made-up data, only the crop calendar."""
    from backend.core.config import settings

    monkeypatch.setattr(settings, "seed_demo_data", False)
    from backend.main import app

    with TestClient(app) as c:
        yield c


def _setup(c):
    return c.post(
        "/officer/setup",
        data=dict(district="Napak", zone="Karamoja agro-pastoral", name="First Officer",
                  phone="0772000111", password="first-pass-1", password2="first-pass-1"),
        follow_redirects=False,
    )


def test_an_empty_app_has_no_farmers_and_asks_for_setup(empty_app):
    r = empty_app.get("/officer/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/officer/setup"
    assert "Karamoja agro-pastoral" in empty_app.get("/officer/setup").text  # zones from the crop calendar


def test_setup_runs_once_and_signs_in_an_admin(empty_app):
    r = _setup(empty_app)
    assert r.status_code == 303 and r.headers["location"] == "/farmers"
    me = empty_app.get("/api/auth/me").json()
    assert me["authenticated"] and me["is_admin"] and me["officer_name"] == "First Officer"
    # A second setup is refused: only an admin adds officers after this.
    other = TestClient(empty_app.app)
    assert other.post("/officer/setup", data={"district": "X"}, follow_redirects=False).headers["location"] == "/officer/login"


def test_officer_registers_a_village_and_a_farmer_who_signs_in_with_the_pin(empty_app):
    _setup(empty_app)
    v = empty_app.post("/api/villages", json={"name": "Lokopo", "latitude": 2.4403, "longitude": 34.3975}).json()
    assert empty_app.post("/api/villages", json={"name": "Nowhere", "latitude": 51.5, "longitude": -0.1}).status_code == 400

    body = {"village_id": v["id"], "group_id": 1, "name": "Akello Susan", "phone": "0701234567", "pin": "4321",
            "consent": False, "crop": "Sorghum", "planted_on": "2026-04-02"}
    assert "must agree" in empty_app.post("/api/farmers", json=body).json()["detail"]
    r = empty_app.post("/api/farmers", json={**body, "consent": True})
    assert r.status_code == 200 and r.json()["code"] == "H-001"
    assert empty_app.post("/api/farmers", json={**body, "consent": True}).status_code == 400  # phone taken

    listed = empty_app.get("/api/farmers").json()
    assert listed[0]["phone"] == "+256701234567" and listed[0]["crop"] == "sorghum" and listed[0]["has_pin"]

    farmer = TestClient(empty_app.app)
    assert farmer.post("/farmer/login", data={"phone": "+256 701 234 567", "pin": "1111"}).status_code == 401
    ok = farmer.post("/farmer/login", data={"phone": "+256 701 234 567", "pin": "4321"}, follow_redirects=False)
    assert ok.status_code == 303
    assert farmer.get("/api/farmer/profile").json()["full_name"] == "Akello Susan"

    # The officer resets the PIN; the old one stops working.
    assert empty_app.post(f"/api/farmers/{listed[0]['household_id']}/pin", json={"pin": "9876"}).status_code == 200
    fresh = TestClient(empty_app.app)
    assert fresh.post("/farmer/login", data={"phone": "0701234567", "pin": "4321"}).status_code == 401
    assert fresh.post("/farmer/login", data={"phone": "0701234567", "pin": "9876"}, follow_redirects=False).status_code == 303


def test_only_an_admin_adds_officers_and_each_signs_in_with_their_own_password(empty_app):
    _setup(empty_app)
    r = empty_app.post("/api/team", json={"name": "Second Officer", "phone": "0772000222", "password": "second-pass"})
    assert r.status_code == 200
    second = TestClient(empty_app.app)
    assert second.post("/api/auth/login", json={"phone": "0772000222", "password": "first-pass-1"}).status_code == 401
    assert second.post("/api/auth/login", json={"phone": "0772000222", "password": "second-pass"}).status_code == 200
    assert second.post("/api/team", json={"name": "X", "phone": "0772000333", "password": "whatever1"}).status_code == 403
    assert second.post("/api/team/me/ussd-pin", json={"pin": "2468"}).status_code == 200


def test_soil_moisture_comes_from_the_open_meteo_model_for_the_village(seeded_session, monkeypatch):
    from backend.core.config import settings
    from backend.tools import agrimoist_tools as impl

    calls = []

    def fake_fetch(lat, lon):
        calls.append((lat, lon))
        return 13.4, "https://api.open-meteo.com/v1/forecast?current=soil_moisture_9_to_27cm"

    monkeypatch.setattr(settings, "soil_source", "open-meteo")
    monkeypatch.setattr(impl, "fetch_modelled_soil_moisture", fake_fetch)
    result = impl.get_soil_moisture(seeded_session, group_id=3)
    by_code = {r["household_code"]: r for r in result["readings"]}
    # Every plot without a current reading gets the modelled value, labelled as such;
    # one request for the village.
    assert len(calls) == 1
    assert by_code["H-010"]["moisture_pct"] == 13.4 and by_code["H-010"]["source"] == impl.SOIL_MODEL_SOURCE
    assert not by_code["H-008"]["stale"]  # the 30-hour-old reading was refreshed
    assert result["plots_without_reading"] == []


def test_soil_model_failure_is_a_gap_not_a_made_up_value(seeded_session, monkeypatch):
    from backend.core.config import settings
    from backend.tools import agrimoist_tools as impl
    from backend.tools.errors import ToolError

    def down(lat, lon):
        raise ToolError("Soil moisture service request failed: timeout")

    monkeypatch.setattr(settings, "soil_source", "open-meteo")
    monkeypatch.setattr(impl, "fetch_modelled_soil_moisture", down)
    with pytest.raises(ToolError):
        impl.get_soil_moisture(seeded_session, group_id=3)


def test_weather_outage_is_never_filled_with_made_up_figures(monkeypatch):
    import httpx

    from backend.tools import agrimoist_tools as impl
    from backend.tools.errors import ToolError

    impl._WEATHER_CACHE.clear()

    def refuse(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(impl.httpx, "get", refuse)
    with pytest.raises(ToolError):
        impl.get_weather(latitude=2.3833, longitude=34.3333)


def test_a_closed_planting_window_always_gets_a_plain_do_not_plant():
    from backend.agent.graph import ensure_planting_instruction

    closed = {"maize": {"planting_window_closed_this_year": True}}
    vague = "The planting windows are closed this year, so planting now will not succeed."
    assert ensure_planting_instruction(vague, closed).endswith("Do not plant maize now: the planting window for this season has closed.")
    assert ensure_planting_instruction("Do not plant maize now: the window has closed.", closed) == (
        "Do not plant maize now: the window has closed."
    )
    assert ensure_planting_instruction("Rain is fine.", {"maize": {"planting_window_closed_this_year": False}}) == "Rain is fine."


def test_the_made_up_seed_never_goes_into_postgres(monkeypatch):
    from sqlmodel import create_engine

    import backend.core.database as database
    from backend.db.seed import seed_allowed

    assert seed_allowed()  # the test database is SQLite
    monkeypatch.setattr(database, "engine", create_engine("postgresql+psycopg://u:p@localhost/x"))
    assert not seed_allowed()


def test_the_advice_also_says_the_window_has_closed():
    from backend.agent.graph import ensure_planting_instruction

    both = {"maize": {"planting_window_closed_this_year": True}, "sorghum": {"planting_window_closed_this_year": True}}
    text = "Do not plant maize now. Do not plant sorghum now. The next planting window opens in March."
    assert ensure_planting_instruction(text, both).endswith("The maize and sorghum planting windows have closed for this season.")
