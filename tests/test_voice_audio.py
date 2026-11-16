# Sunbird text-to-speech for the farmer page's voice reader. No real Sunbird call:
# the conftest clears the key, and the HTTP calls are replaced where a key is needed.
from __future__ import annotations

import pytest

from backend.agent import model_client
from backend.core.config import settings
from backend.farmer import router as farmer_router

WAV = b"RIFF fake wav bytes"


@pytest.fixture
def voice_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(farmer_router, "VOICE_CACHE_DIR", tmp_path / "voice_cache")
    return tmp_path / "voice_cache"


@pytest.fixture
def fake_sunbird(monkeypatch):
    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    calls = {"speech": [], "download": []}

    class FakeResponse:
        def __init__(self, body=None, content=b""):
            self._body, self.content = body, content

        def raise_for_status(self):
            pass

        def json(self):
            return self._body

    def fake_post(url, headers, json, timeout):
        calls["speech"].append((url, json))
        return FakeResponse({"audio_url": "https://storage.example/a.wav", "voice": json["voice"]})

    def fake_get(url, timeout):
        calls["download"].append(url)
        return FakeResponse(content=WAV)

    monkeypatch.setattr(model_client.httpx, "post", fake_post)
    monkeypatch.setattr(model_client.httpx, "get", fake_get)
    return calls


def test_voice_audio_is_503_without_a_sunbird_key(client, voice_dir):
    res = client.post("/api/farmer/voice-audio", json={"text": "Welcome", "lang": "en"})
    assert res.status_code == 503


def test_voice_audio_uses_the_ateso_voice_for_karamojong(client, voice_dir, fake_sunbird):
    res = client.post("/api/farmer/voice-audio", json={"text": "Ejok nooi", "lang": "karamojong"})
    assert res.status_code == 200, res.text
    url, body = fake_sunbird["speech"][0]
    assert url.endswith("/tasks/audio/speech")
    assert body["language"] == "teo" and body["voice"] == "salt_teo_0001"
    assert body["text"] == "Ejok nooi"
    assert res.json()["voice"] == "salt_teo_0001"


def test_voice_audio_is_generated_once_then_served_by_the_app(client, voice_dir, fake_sunbird):
    first = client.post("/api/farmer/voice-audio", json={"text": "Karibu", "lang": "sw"}).json()
    again = client.post("/api/farmer/voice-audio", json={"text": "Karibu", "lang": "sw"}).json()
    assert first["audio_url"] == again["audio_url"]
    assert first["audio_url"].startswith("/api/farmer/voice-audio/")
    assert len(fake_sunbird["speech"]) == 1 and len(fake_sunbird["download"]) == 1

    audio = client.get(first["audio_url"])
    assert audio.status_code == 200
    assert audio.content == WAV
    assert audio.headers["content-type"] == "audio/wav"
    assert "immutable" in audio.headers["cache-control"]  # phones keep it


def test_voice_audio_file_route_rejects_other_paths(client, voice_dir):
    assert client.get("/api/farmer/voice-audio/not-a-voice-file.txt").status_code == 404
    assert client.get("/api/farmer/voice-audio/" + "0" * 32 + ".wav").status_code == 404


def test_voice_audio_failure_is_503(client, voice_dir, monkeypatch):
    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")

    def broken_post(*args, **kwargs):
        raise RuntimeError("sunbird down")

    monkeypatch.setattr(model_client.httpx, "post", broken_post)
    res = client.post("/api/farmer/voice-audio", json={"text": "Karibu", "lang": "sw"})
    assert res.status_code == 503
    assert not voice_dir.exists() or not any(voice_dir.iterdir())


def test_a_slow_piece_answers_pending_instead_of_holding_up_the_reading(client, voice_dir, monkeypatch):
    # Sunbird can take over a minute per piece; the page waits only a few seconds.
    import threading

    release = threading.Event()
    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    monkeypatch.setattr(model_client, "synthesize_speech", lambda text, lang: release.wait(10) and {"audio_url": "u"})
    monkeypatch.setattr(model_client, "download_audio", lambda url: WAV)

    slow = client.post("/api/farmer/voice-audio", json={"text": "Slow piece", "lang": "kdj", "wait": 0.2})
    assert slow.status_code == 202 and slow.json() == {"pending": True}
    release.set()
    # The audio keeps being made in the background; asking again gets it.
    ready = client.post("/api/farmer/voice-audio", json={"text": "Slow piece", "lang": "kdj", "wait": 5})
    assert ready.status_code == 200 and ready.json()["audio_url"].startswith("/api/farmer/voice-audio/")


def test_opening_the_page_starts_making_every_piece_of_its_audio(farmer_client, voice_dir, fake_sunbird):
    farmer = farmer_client("+256700000004")
    script = farmer.get("/api/farmer/voice-script", params={"lang": "kdj", "view": "dashboard"}).json()
    assert script["pieces"] == farmer_router.split_for_speech(script["sentences"])
    for job in list(farmer_router._VOICE_JOBS.values()):
        job.result(timeout=10)
    made = {p.name for p in voice_dir.glob("*.wav")}
    assert {farmer_router._voice_file_name("kdj", p["text"]) for p in script["pieces"]} <= made


def test_speech_pieces_keep_decimals_whole_and_start_with_one_sentence():
    pieces = farmer_router.split_for_speech(
        [{"label": "a", "text": "Rain was 58.4 mm. Next week 0.6 mm is forecast. Soil is dry."}]
    )
    assert pieces[0]["text"] == "Rain was 58.4 mm."
    assert pieces[1]["text"] == "Next week 0.6 mm is forecast. Soil is dry."
