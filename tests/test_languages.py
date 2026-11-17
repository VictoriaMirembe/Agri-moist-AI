# Farmer languages: Luganda and Runyankole beside Karamojong and Swahili. Every text
# is translated once by Sunbird and stored; advice is translated ahead of time on
# its own queue.
from __future__ import annotations

import threading

import pytest

from backend.agent import model_client, translations
from backend.core.config import settings


@pytest.fixture()
def store(test_db):
    test_db.init_db()
    return test_db


def _fake(monkeypatch, sunbird=None, groq=None):
    """sunbird/groq: functions (text, lang) -> translated text or None."""
    if sunbird is not None:
        monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
        monkeypatch.setattr(model_client, "_translate_with_sunbird",
                            lambda text, code: {"translated_text": sunbird(text, code), "model": "sunbird/sunflower-9b"})
    if groq is not None:
        monkeypatch.setattr(settings, "groq_api_key", "test-key")
        monkeypatch.setattr(model_client, "_translate_with_groq",
                            lambda text, name: {"translated_text": groq(text, name), "model": settings.groq_model})


def test_numbers_and_names_become_placeholders_so_one_translation_serves_everyone():
    a, slots_a = translations.protect("Welcome to your farm, Akello Susan. Rain was 58.4 mm.", ("Akello Susan",))
    b, _ = translations.protect("Welcome to your farm, Lomuria Peter. Rain was 3.0 mm.", ("Lomuria Peter",))
    assert a == b == "Welcome to your farm, [A]. Rain was [B]."
    assert slots_a == {"[A]": "Akello Susan", "[B]": "58.4 mm"}


def test_a_translation_that_loses_a_placeholder_or_adds_a_number_is_refused():
    assert translations._usable("Rain was [A].", "Enkuba yali [A].")
    assert not translations._usable("Rain was [A].", "Enkuba yali.")
    assert not translations._usable("Rain was [A].", "Enkuba yali [A] [A].")
    assert not translations._usable("Rain was [A].", "Enkuba yali [A] ne 7.")


def test_stored_once_then_served_with_the_real_values(store, monkeypatch):
    calls = []

    def sunbird(text, code):
        calls.append(code)
        return text.replace("Rain was", "Enkuba yali")

    _fake(monkeypatch, sunbird=sunbird)
    assert translations.translate_now("Rain was 58.4 mm.", "lug", wait=5) == "Enkuba yali 58.4 mm."
    # Another day's figure: the stored translation is reused, no new call.
    assert translations.lookup("Rain was 3.0 mm.", "lug") == "Enkuba yali 3.0 mm."
    assert calls == ["lug"]


def test_only_sunbird_translates_and_a_failure_shows_english_not_a_guess(store, monkeypatch):
    # The Groq model's Luganda and Runyankole were often wrong, so it is never used here.
    used = []
    _fake(monkeypatch, sunbird=lambda text, code: None, groq=lambda text, name: used.append(name) or "guess")
    assert translations.translate_now("Water plot H-004.", "lug", wait=5) is None
    assert used == []


def test_advice_goes_on_its_own_queue_ahead_of_page_sentences(store, monkeypatch):
    release = threading.Event()
    seen = []

    def sunbird(text, code):
        seen.append(text)
        if text.startswith("Page"):
            release.wait(10)
        return "[lug] " + text

    _fake(monkeypatch, sunbird=sunbird)
    for i in range(6):  # fills the page queue
        translations.ensure(f"Page sentence {chr(65 + i)}.", "lug")
    translations.ensure("Weed the sorghum.", "lug")  # also waiting on the busy page queue
    job = translations.ensure("Weed the sorghum.", "lug", priority=True)
    if job is not None:
        job.result(timeout=5)
    assert translations.lookup("Weed the sorghum.", "lug") == "[lug] Weed the sorghum."
    release.set()


def test_farmer_sees_advice_in_their_language_and_english_while_it_is_translated(client, farmer_client, monkeypatch):
    advice = client.get("/api/approvals", params={"status": "pending_review"}).json()[0]
    assert client.post(f"/api/approvals/{advice['id']}/approve", json={}).status_code == 200
    farmer = farmer_client("+256700000004")
    english = farmer.get("/api/farmer/advice", params={"lang": "en"}).json()

    # No translator: English, and not claiming to translate.
    plain = farmer.get("/api/farmer/advice", params={"lang": "nyn"}).json()
    assert plain["text"] == english["text"] and plain["language"] == "English" and not plain["translating"]

    _fake(monkeypatch, sunbird=lambda text, code: "[nyn] " + text)
    first = farmer.get("/api/farmer/advice", params={"lang": "nyn"}).json()
    assert first["text"] == english["text"] and first["translating"]  # started in the background
    for job in list(translations._JOBS.values()):
        job.result(timeout=10)
    again = farmer.get("/api/farmer/advice", params={"lang": "nyn"}).json()
    assert again["text"] == "[nyn] " + english["text"] and again["language"] == "Runyankole"
    assert not again["translating"]


def test_the_farmer_choice_of_language_is_remembered(farmer_client):
    from sqlmodel import select

    from backend.core.database import session_scope
    from backend.db.models import FarmerLanguage

    farmer = farmer_client("+256700000004")
    assert farmer.post("/api/farmer/language", json={"lang": "Luganda"}).json() == {"lang": "lug"}
    with session_scope() as session:
        assert [r.lang for r in session.exec(select(FarmerLanguage)).all()] == ["lug"]


def test_luganda_voice_script_reads_untranslated_sentences_in_english_for_now(farmer_client):
    farmer = farmer_client("+256700000004")
    script = farmer.get("/api/farmer/voice-script", params={"lang": "lug", "view": "report"}).json()
    assert script["language"] == "lug" and script["language_label"] == "Luganda"
    assert all(p["lang"] == "en" for p in script["pieces"])  # no translator in tests
