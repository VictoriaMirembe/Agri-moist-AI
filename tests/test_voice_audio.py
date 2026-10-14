# Sunbird text-to-speech for the farmer page's voice reader. No real Sunbird call:
# the conftest clears the key, and the call itself is replaced where a key is needed.
from __future__ import annotations

from backend.agent import model_client
from backend.core.config import settings
from backend.farmer import router as farmer_router


def test_voice_audio_is_503_without_a_sunbird_key(client):
    res = client.post("/api/farmer/voice-audio", json={"text": "Welcome", "lang": "en"})
    assert res.status_code == 503


def test_voice_audio_uses_the_ateso_voice_for_karamojong(client, monkeypatch):
    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    farmer_router._VOICE_AUDIO_CACHE.clear()
    sent = []

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"audio_url": "https://storage.example/a.wav", "voice": "salt_teo_0001", "duration_seconds": 2.0}

    def fake_post(url, headers, json, timeout):
        sent.append((url, json))
        return FakeResponse()

    monkeypatch.setattr(model_client.httpx, "post", fake_post)

    for _ in range(2):  # the second request is served from the cache
        res = client.post("/api/farmer/voice-audio", json={"text": "Ejok nooi", "lang": "karamojong"})
        assert res.status_code == 200, res.text
        assert res.json()["audio_url"] == "https://storage.example/a.wav"

    assert len(sent) == 1
    url, body = sent[0]
    assert url.endswith("/tasks/audio/speech")
    assert body["language"] == "teo" and body["voice"] == "salt_teo_0001"
    assert body["text"] == "Ejok nooi"


def test_voice_audio_failure_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    farmer_router._VOICE_AUDIO_CACHE.clear()

    def broken_post(*args, **kwargs):
        raise RuntimeError("sunbird down")

    monkeypatch.setattr(model_client.httpx, "post", broken_post)
    res = client.post("/api/farmer/voice-audio", json={"text": "Karibu", "lang": "sw"})
    assert res.status_code == 503


def test_expired_voice_audio_is_re_signed_not_generated_again(client, monkeypatch):
    monkeypatch.setattr(settings, "sunbird_api_key", "test-key")
    farmer_router._VOICE_AUDIO_CACHE.clear()
    key = ("en", "Welcome")
    old = {"audio_url": "https://storage.example/old.wav", "voice": "salt_eng_0001", "gcs_object": "orpheus_tts/x.wav"}
    farmer_router._VOICE_AUDIO_CACHE[key] = (-10_000.0, old)  # long past the 25-minute reuse window

    def no_generation(*args, **kwargs):
        raise AssertionError("audio was generated again instead of re-signed")

    monkeypatch.setattr(model_client, "synthesize_speech", no_generation)
    monkeypatch.setattr(model_client, "refresh_speech_url", lambda gcs_object: "https://storage.example/fresh.wav")

    res = client.post("/api/farmer/voice-audio", json={"text": "Welcome", "lang": "en"})
    assert res.status_code == 200, res.text
    assert res.json()["audio_url"] == "https://storage.example/fresh.wav"
