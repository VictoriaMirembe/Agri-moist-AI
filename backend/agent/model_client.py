# The open-weights model client. Uses Groq's OpenAI-compatible API when GROQ_API_KEY
# is set in .env. With no key, every function falls back to a deterministic,
# rule-based mode so the whole flow still runs end to end and can be marked
# "rule-based-stub" honestly in the run log - never a made-up "real run" (README "Safety rules").
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from openai import OpenAI

from backend.core.config import settings

_client: OpenAI | None = None

# Published Groq per-token prices, USD per 1M tokens (input, output). Used only to
# estimate a cost figure for the log; update if the model or pricing changes.
_PRICES_PER_MILLION = {
    "openai/gpt-oss-120b": (0.15, 0.60),  # console.groq.com/docs/model/openai/gpt-oss-120b
    "llama-3.3-70b-versatile": (0.59, 0.79),
}
_DEFAULT_PRICE = (0.60, 0.80)
# Sunbird AI translation, USD per 1M tokens (GET https://api.sunbird.ai/api/billing/rates).
_SUNBIRD_TRANSLATION_PRICE_PER_MILLION = 1.0


def _get_client() -> OpenAI | None:
    global _client
    if not settings.model_configured:
        return None
    if _client is None:
        _client = OpenAI(api_key=settings.groq_api_key, base_url=settings.groq_base_url)
    return _client


def _estimate_cost(usage: Any) -> float:
    if usage is None:
        return 0.0
    price_in, price_out = _PRICES_PER_MILLION.get(settings.groq_model, _DEFAULT_PRICE)
    cost = (usage.prompt_tokens / 1_000_000) * price_in + (usage.completion_tokens / 1_000_000) * price_out
    return round(cost, 6)


DRAFT_SYSTEM_PROMPT = (
    "You are AgriMoist AI, drafting field advice for a Ugandan agricultural extension "
    "officer in Karamoja to review before anything reaches a farmer. You draft only; "
    "you never send anything to a farmer, order inputs or request credit. Use only "
    "the facts you are given, each already tied to a source. If a needed fact is "
    "marked as a gap or missing, say so plainly in the advice - never guess or invent "
    "a reading, a date or a forecast. Only write numbers that appear in the facts, "
    "copied exactly - a draft with any other number is blocked before review. "
    "Start with the rain and soil-moisture signal (recent rain, the forecast and soil "
    "wetness) and what it means for watering; only then cover pests and planting. "
    "Never put internal ids or field names in the advice or the why text - no "
    "'observation:12', 'plant_to', 'planting_window_closed_this_year', "
    "'pest_reports_list' or source references; the officer already sees the sources. "
    "Write dates in plain words, such as 'end of May'. "
    "Never recommend pesticides, chemicals, doses, fertiliser or switching to another "
    "crop unless one of the facts supports it; if action may be needed, tell the "
    "farmer to contact their extension officer. "
    "Keep the advice short and in plain words a "
    "farmer could act on. Reply as JSON only: "
    '{"advice": "...", "why": "one or two sentences citing the facts used"}'
)


def draft_advice_text(facts: dict[str, Any]) -> dict[str, Any]:
    client = _get_client()
    if client is None:
        return _stub_draft(facts)
    try:
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {"role": "system", "content": DRAFT_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(facts, default=str)},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(response.choices[0].message.content)
        advice = (parsed.get("advice") or "").strip()
        why = (parsed.get("why") or "").strip()
        if not advice or not why:
            raise ValueError("model reply missing advice or why")
        return {
            "advice": advice,
            "why": why,
            "model": settings.groq_model,
            "cost": _estimate_cost(response.usage),
        }
    except Exception as exc:  # noqa: BLE001 - any model failure falls back to the stub
        stub = _stub_draft(facts)
        stub["model"] = f"rule-based-stub (model call failed: {exc})"
        return stub


def _stub_draft(facts: dict[str, Any]) -> dict[str, Any]:
    """Deterministic, rule-based drafting used whenever no model key is configured,
    or the model call fails. Every line traces back to a fact already in `facts`."""
    lines: list[str] = []
    weather = facts.get("weather") or {}

    if "gap" in weather:
        lines.append("Weather data is missing after a retry - check rain before approving this advice.")
    else:
        past = weather.get("past_7_days_rain_mm", 0)
        upcoming = weather.get("next_7_days_rain_mm", 0)
        if upcoming is not None and upcoming >= 15:
            lines.append(f"Heavy rain is forecast ({upcoming}mm over the next 7 days); no extra watering is needed this week.")
        elif past is not None and past < 5:
            lines.append(f"Little rain fell in the last 7 days ({past}mm); water this week if soil is dry.")
        else:
            lines.append(f"Rain has been light to moderate recently ({past}mm past week, {upcoming}mm forecast); watch soil before watering.")

    field_records = (facts.get("field_records") or {}).get("records", [])
    dry_recent = [r["household_code"] for r in field_records if r.get("soil_wetness") == "dry"]
    if dry_recent:
        lines.append(f"Soil was dry at the last visit for: {', '.join(sorted(set(dry_recent)))}.")

    pest_names = sorted({r["pest"] for r in (facts.get("pest_reports_list") or [])})
    if pest_names:
        lines.append(f"Pests reported nearby: {', '.join(pest_names)}. Check the crop for early signs.")

    for crop, cal in (facts.get("crop_calendar") or {}).items():
        if cal.get("planting_window_closed_this_year"):
            lines.append(
                f"The planting window for {crop} ({cal['plant_from']} to {cal['plant_to']}) has closed for this "
                f"year; do not advise planting {crop} now."
            )

    advice = " ".join(lines) if lines else "No clear signal from the data gathered; an officer should check in person."
    why = "Based on " + ", ".join(
        sorted(
            {
                s
                for s in [
                    "the rain forecast" if weather else None,
                    "recent visit records" if field_records else None,
                    "pest reports" if pest_names else None,
                    "the crop calendar" if facts.get("crop_calendar") else None,
                ]
                if s
            }
        )
        or ["the facts gathered for this group"]
    ) + "."
    return {"advice": advice, "why": why, "model": "rule-based-stub", "cost": 0.0}


CANNOT_TRANSLATE = "CANNOT_TRANSLATE"
_REFUSAL_STARTS = ("i'm sorry", "i’m sorry", "i am sorry", "sorry", "i cannot", "i can't", "i can’t", "i don't", "i don’t", "as an ai")


def _is_refusal(text: str) -> bool:
    lowered = text.strip().lower()
    return CANNOT_TRANSLATE.lower() in lowered or lowered.startswith(_REFUSAL_STARTS)


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:\.\d+)?", text))


def translate_text(text: str, target_language: str) -> dict[str, Any]:
    """Translate approved advice for farmers: Sunbird AI's sunflower-9b first (it is
    trained on Ugandan languages, Karamojong included), then the Groq model as a
    backup. Returns translated_text=None when no usable translation came back (no
    key, a failed call, a refusal, or a translation that changed or dropped a number)
    - callers then show the English original rather than mislabel it as translated."""
    if not text:
        return {"translated_text": None, "model": "none (nothing to translate)", "cost": 0.0}
    attempts: list[str] = []
    cost = 0.0  # a failed or rejected attempt can still have used paid tokens
    # Sunbird's output varies between calls (it sometimes writes a number out in
    # words), so it gets a second try before the backup.
    for translate in (_translate_with_sunbird, _translate_with_sunbird, _translate_with_groq):
        result = translate(text, target_language)
        if result is None:
            continue  # that service has no key configured
        if result["translated_text"] and _numbers(result["translated_text"]) != _numbers(text):
            result = {
                **result,
                "translated_text": None,
                "model": f"fallback ({result['model']} changed a number; showing English)",
            }
        cost += result["cost"]
        if result["translated_text"]:
            return {**result, "cost": round(cost, 6)}
        attempts.append(result["model"])
    if not attempts:
        return {"translated_text": None, "model": "none (no translator key configured; showing English)", "cost": 0.0}
    return {"translated_text": None, "model": "; ".join(attempts), "cost": round(cost, 6)}


def _translate_with_sunbird(text: str, target_language: str) -> dict[str, Any] | None:
    if not settings.translator_configured:
        return None
    target = settings.farmer_language_code if target_language == settings.farmer_language else target_language
    try:
        response = httpx.post(
            settings.sunbird_api_url.rstrip("/") + "/tasks/translate",
            headers={"Authorization": f"Bearer {settings.sunbird_api_key}"},
            json={"source_language": "eng", "target_language": target, "text": text},
            timeout=60,
        )
        response.raise_for_status()
        body = response.json()
        output = body.get("output") or {}
        tokens = (body.get("usage") or {}).get("total_tokens") or 0
        cost = round(tokens / 1_000_000 * _SUNBIRD_TRANSLATION_PRICE_PER_MILLION, 6)
        translated = (output.get("translated_text") or "").strip()
        if output.get("Error") or not translated or translated == text.strip():
            return {
                "translated_text": None,
                "model": f"fallback (sunbird could not translate: {output.get('Error') or 'empty reply'})",
                "cost": cost,
            }
        return {"translated_text": translated, "model": "sunbird/sunflower-9b", "cost": cost}
    except Exception as exc:  # noqa: BLE001 - any failure falls through to the backup
        return {"translated_text": None, "model": f"fallback (sunbird translation failed: {exc})", "cost": 0.0}


# Sunbird text-to-speech voices (GET /tasks/voice/speakers). Sunbird has no
# Karamojong voice, so Karamojong text is read by the Ateso voice: both are
# Teso-Turkana languages, close enough that it sounds far more natural than a
# generic browser voice.
SUNBIRD_VOICES = {
    "en": ("eng", "salt_eng_0001"),
    "sw": ("swa", "waxal_swa_0006"),
    "kdj": ("teo", "salt_teo_0001"),
}


def synthesize_speech(text: str, lang: str) -> dict[str, Any] | None:
    """Read text aloud with Sunbird AI's Orpheus TTS. Returns the signed audio URL
    (valid for 30 minutes), or None when there is no key, the language has no
    voice, or the call fails - the farmer page then falls back to the browser voice."""
    if not settings.translator_configured or not text.strip() or lang not in SUNBIRD_VOICES:
        return None
    language, voice = SUNBIRD_VOICES[lang]
    try:
        response = httpx.post(
            settings.sunbird_api_url.rstrip("/") + "/tasks/audio/speech",
            headers={"Authorization": f"Bearer {settings.sunbird_api_key}"},
            json={"text": text.strip(), "language": language, "voice": voice, "response_mode": "url"},
            timeout=90,
        )
        response.raise_for_status()
        body = response.json()
        if not body.get("audio_url"):
            return None
        return {
            "audio_url": body["audio_url"],
            "voice": body.get("voice", voice),
            "duration_seconds": body.get("duration_seconds"),
            "gcs_object": body.get("gcs_object"),
        }
    except Exception:  # noqa: BLE001 - any failure falls back to the browser voice
        return None


def download_audio(url: str) -> bytes | None:
    """The generated audio itself, so the app can keep it and serve it (Sunbird's
    storage links expire after 30 minutes and cannot be cached by browsers)."""
    try:
        response = httpx.get(url, timeout=60)
        response.raise_for_status()
        return response.content or None
    except Exception:  # noqa: BLE001
        return None


def _translate_with_groq(text: str, target_language: str) -> dict[str, Any] | None:
    client = _get_client()
    if client is None:
        return None
    try:
        response = client.chat.completions.create(
            model=settings.groq_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"Translate the following farm advice into {target_language}. Keep the "
                        "meaning exact and the words simple. Reply with only the translation, "
                        f"no notes. If you cannot translate it accurately, reply with exactly {CANNOT_TRANSLATE}."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.0,
        )
        cost = _estimate_cost(response.usage)  # tokens are paid for even if the reply is unusable
        translated = (response.choices[0].message.content or "").strip()
        if not translated or _is_refusal(translated):
            # Never store a refusal or apology as the "translation" - the farmer would
            # see it instead of their advice. Fall back to the English text.
            return {
                "translated_text": None,
                "model": f"fallback (model could not translate: {translated[:80]!r}; showing English)",
                "cost": cost,
            }
        return {"translated_text": translated, "model": settings.groq_model, "cost": cost}
    except Exception as exc:  # noqa: BLE001
        return {"translated_text": None, "model": f"fallback (translation failed: {exc}; showing English)", "cost": 0.0}
