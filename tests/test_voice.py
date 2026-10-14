# Tests for voice audio output feature in English, Karamojong, and Swahili
from __future__ import annotations

import pytest


def test_voice_script_unauthenticated_or_signin(client):
    for lang, expected_word in (
        ("en", "Welcome"),
        ("sw", "Karibu"),
        ("kdj", "Ejok nooi"),
    ):
        res = client.get("/api/farmer/voice-script", params={"lang": lang, "view": "signin"})
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["language"] == ("sw" if lang == "sw" else ("kdj" if lang == "kdj" else "en"))
        assert expected_word in data["full_script"]


def test_voice_script_authenticated_dashboard(client):
    for lang, expected_snippet in (
        ("en", "Welcome to your farm dashboard"),
        ("sw", "Karibu kwenye shamba lako"),
        ("kdj", "Ejok nooi amana kon"),
    ):
        res = client.get(
            "/api/farmer/voice-script",
            params={"phone": "+256700000001", "lang": lang, "view": "dashboard"},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert expected_snippet in data["full_script"]
        # Registered farmer name is mentioned
        assert "Akello Susan" in data["full_script"]
        # Soil condition or field conditions are included
        assert len(data["sentences"]) >= 3


def test_voice_script_different_views(client):
    # Report view in Swahili
    rep_sw = client.get("/api/farmer/voice-script", params={"lang": "sw", "view": "report"}).json()
    assert "Ripoti tatizo" in rep_sw["full_script"]

    # Report view in Karamojong
    rep_kdj = client.get("/api/farmer/voice-script", params={"lang": "kdj", "view": "report"}).json()
    assert "Elimo" in rep_kdj["full_script"]

    # Visit view in Swahili
    vis_sw = client.get("/api/farmer/voice-script", params={"lang": "sw", "view": "visit"}).json()
    assert "akutembelee" in vis_sw["full_script"].lower()

    # Advice view in English for registered farmer
    adv_en = client.get(
        "/api/farmer/voice-script",
        params={"phone": "+256700000001", "lang": "en", "view": "advice"},
    ).json()
    assert "advice" in adv_en["full_script"].lower()


def test_farmer_home_page_contains_professional_language_selector_and_voice_suite(client):
    res = client.get("/farmer/home", params={"phone": "+256700000001"})
    assert res.status_code == 200
    html = res.text

    # Topbar contains the voice toolbar
    assert 'class="farmer-voice-toolbar"' in html
    # Language dropdown with English, Karamojong, and Swahili options
    assert 'id="farmer-audio-lang"' in html
    assert 'value="en">English' in html
    assert 'value="kdj">Karamojong' in html
    assert 'value="sw">Swahili' in html

    # Listen button in the topbar
    assert 'id="farmer-listen-btn"' in html
    assert 'id="farmer-voice-btn-label"' in html
    assert 'id="farmer-voice-waves"' in html

    # Floating Audio Player bar
    assert 'id="farmer-audio-player-bar"' in html
    assert 'id="farmer-audio-play-pause"' in html
    assert 'id="farmer-audio-speed"' in html
    assert 'id="farmer-audio-live-text"' in html

    # Section-level audio listen buttons
    assert 'data-voice-section="conditions"' in html
    assert 'data-voice-section="advice"' in html
