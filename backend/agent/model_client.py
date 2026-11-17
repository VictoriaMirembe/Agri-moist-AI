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


# Token counts reported next to the cost on the run page and in the activity log.
# A stub or failed call has no usage object, so every number is 0 - never an estimate.
ZERO_TOKENS = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _usage_tokens(usage: Any) -> dict[str, int]:
    """The provider's own token counts, from an OpenAI-style usage object or a JSON
    dict. Missing numbers stay 0; total_tokens falls back to prompt + completion
    only when the provider did not send a total of its own."""
    if usage is None:
        return dict(ZERO_TOKENS)

    def field(name: str) -> int:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, 0)
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    prompt, completion = field("prompt_tokens"), field("completion_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": field("total_tokens") or prompt + completion,
    }


def _add_tokens(running: dict[str, int], result: dict[str, Any]) -> None:
    """Add one attempt's tokens to the running total for a translation."""
    for name in ZERO_TOKENS:
        running[name] += int(result.get(name) or 0)


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
    "Soil moisture readings are described by their source; use only current_readings, and "
    "never give a value for a reading listed as stale (out of date). Quote a sensor "
    "reading exactly as given, decimal included (write 11.2%, never 'about 11%'): a "
    "rounded figure matches no source and the draft is blocked. "
    "If heavy_rain_warning is present, follow it: do not tell farmers to water, "
    "irrigate or add water, even for dry plots - a draft that does is blocked. "
    "If a crop's planting window has closed this year, say so and give a plain "
    "instruction in these words: 'Do not plant <crop> now.' Then say when the next "
    "planting window opens, in plain words. "
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
            **_usage_tokens(response.usage),
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
        if upcoming is not None and upcoming >= settings.heavy_rain_threshold_mm:
            lines.append(f"Heavy rain is forecast ({upcoming}mm over the next 7 days); no extra watering is needed this week.")
        elif past is not None and past < 5:
            lines.append(f"Little rain fell in the last 7 days ({past}mm); water this week if soil is dry.")
        else:
            lines.append(f"Rain has been light to moderate recently ({past}mm past week, {upcoming}mm forecast); watch soil before watering.")

    soil = facts.get("soil_moisture") or {}
    sensor_current = soil.get("current_readings") or []
    if "gap" in soil:
        lines.append("Soil sensor readings are missing after a retry - check the soil before approving this advice.")
    else:
        dry_sensor = [r for r in sensor_current if r["moisture_pct"] < 15]
        if dry_sensor:
            plots = ", ".join(f"{r['household_code']} ({r['moisture_pct']}%)" for r in dry_sensor)
            lines.append(f"The simulated soil sensor shows dry soil at {plots}.")
        stale_codes = [r["household_code"] for r in soil.get("stale_readings_not_used") or []]
        if stale_codes:
            lines.append(f"The sensor reading for {', '.join(stale_codes)} is out of date, so it was not used.")

    field_records = (facts.get("field_records") or {}).get("records", [])
    dry_recent =[r["household_code"] for r in field_records if r.get("soil_wetness") == "dry"]
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
                    "simulated soil sensor readings" if sensor_current else None,
                    "recent visit records" if field_records else None,
                    "pest reports" if pest_names else None,
                    "the crop calendar" if facts.get("crop_calendar") else None,
                ]
                if s
            }
        )
        or ["the facts gathered for this group"]
    ) + "."
    return {"advice": advice, "why": why, "model": "rule-based-stub", "cost": 0.0, **ZERO_TOKENS}


CANNOT_TRANSLATE = "CANNOT_TRANSLATE"
_REFUSAL_STARTS = ("i'm sorry", "i’m sorry", "i am sorry", "sorry", "i cannot", "i can't", "i can’t", "i don't", "i don’t", "as an ai")


def _is_refusal(text: str) -> bool:
    lowered = text.strip().lower()
    return CANNOT_TRANSLATE.lower() in lowered or lowered.startswith(_REFUSAL_STARTS)


def _numbers(text: str) -> list[str]:
    return sorted(re.findall(r"\d+(?:\.\d+)?", text))


# Numbers and plot codes are swapped for placeholders like [A] before translating and
# put back after. Sunbird otherwise often writes a number out in Karamojong words, and
# the number check then has to reject the whole translation; tried on 2026-11-17, a
# placeholder survived 3 of 3 times where the plain "7" survived 1 of 3.
_PROTECT = re.compile(r"\b[A-Z]-\d+\b|\d+(?:\.\d+)?(?:\s?(?:mm|%))?")


def _protect_numbers(text: str) -> tuple[str, dict[str, str]]:
    slots: dict[str, str] = {}

    def swap(match: re.Match) -> str:
        n = len(slots)
        key = f"[{chr(65 + n % 26)}{n // 26 or ''}]"
        slots[key] = match.group(0)
        return key

    return _PROTECT.sub(swap, text), slots


def _restore_numbers(text: str, slots: dict[str, str]) -> str:
    for key, value in slots.items():
        text = text.replace(key, value)
    return text


def translate_text(text: str, target_language: str) -> dict[str, Any]:
    """Translate approved advice for farmers: Sunbird AI's sunflower-9b first (it is
    trained on Ugandan languages, Karamojong included), then the Groq model as a
    backup. Returns translated_text=None when no usable translation came back (no
    key, a failed call, a refusal, or a translation that changed or dropped a number)
    - callers then show the English original rather than mislabel it as translated."""
    if not text:
        return {"translated_text": None, "model": "none (nothing to translate)", "cost": 0.0, **ZERO_TOKENS}
    attempts: list[str] = []
    cost = 0.0  # a failed or rejected attempt can still have used paid tokens
    tokens = dict(ZERO_TOKENS)  # and its tokens still count towards this translation
    # Sunbird's output varies between calls (it sometimes writes a number out in
    # words), so it gets a second try before the backup.
    protected, slots = _protect_numbers(text)
    for translate in (_translate_with_sunbird, _translate_with_sunbird, _translate_with_groq):
        result = translate(protected, target_language)
        if result is None:
            continue  # that service has no key configured
        if result["translated_text"]:
            # Put the exact numbers back. A placeholder the translator dropped or
            # repeated leaves the numbers different, and the check below rejects it.
            result = {**result, "translated_text": _restore_numbers(result["translated_text"], slots)}
        if result["translated_text"] and _numbers(result["translated_text"]) != _numbers(text):
            result = {
                **result,
                "translated_text": None,
                "model": f"fallback ({result['model']} changed a number; showing English)",
            }
        cost += result["cost"]
        _add_tokens(tokens, result)
        if result["translated_text"]:
            return {**result, "cost": round(cost, 6), **tokens}
        attempts.append(result["model"])
    if not attempts:
        return {
            "translated_text": None,
            "model": "none (no translator key configured; showing English)",
            "cost": 0.0,
            **ZERO_TOKENS,
        }
    return {"translated_text": None, "model": "; ".join(attempts), "cost": round(cost, 6), **tokens}


def _translate_with_sunbird(text: str, target_language: str) -> dict[str, Any] | None:
    if not settings.translator_configured:
        return None
    target = settings.farmer_language_code if target_language == settings.farmer_language else target_language
    try:
        response = httpx.post(
            settings.sunbird_api_url.rstrip("/") + "/tasks/translate",
            headers={"Authorization": f"Bearer {settings.sunbird_api_key}"},
            json={"source_language": "eng", "target_language": target, "text": text},
            timeout=120,
        )
        response.raise_for_status()
        body = response.json()
        output = body.get("output") or {}
        usage = _usage_tokens(body.get("usage"))
        billed_tokens = (body.get("usage") or {}).get("total_tokens") or 0
        cost = round(billed_tokens / 1_000_000 * _SUNBIRD_TRANSLATION_PRICE_PER_MILLION, 6)
        translated = (output.get("translated_text") or "").strip()
        if output.get("Error") or not translated or translated == text.strip():
            return {
                "translated_text": None,
                "model": f"fallback (sunbird could not translate: {output.get('Error') or 'empty reply'})",
                "cost": cost,
                **usage,
            }
        return {"translated_text": translated, "model": "sunbird/sunflower-9b", "cost": cost, **usage}
    except Exception as exc:  # noqa: BLE001 - any failure falls through to the backup
        return {
            "translated_text": None,
            "model": f"fallback (sunbird translation failed: {exc})",
            "cost": 0.0,
            **ZERO_TOKENS,
        }


# Sunbird text-to-speech voices (GET /tasks/voice/speakers). Sunbird has no
# Karamojong voice, so Karamojong text is read by the Ateso voice: both are
# Teso-Turkana languages, close enough that it sounds far more natural than a
# generic browser voice.
SUNBIRD_VOICES = {
    "en": ("eng", "salt_eng_0001"),
    "sw": ("swa", "waxal_swa_0006"),
    "kdj": ("teo", "salt_teo_0001"),
    "lug": ("lug", "Namuli"),
    "nyn": ("nyn", "Kabasinga"),
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
        usage = _usage_tokens(response.usage)
        translated = (response.choices[0].message.content or "").strip()
        if not translated or _is_refusal(translated):
            # Never store a refusal or apology as the "translation" - the farmer would
            # see it instead of their advice. Fall back to the English text.
            return {
                "translated_text": None,
                "model": f"fallback (model could not translate: {translated[:80]!r}; showing English)",
                "cost": cost,
                **usage,
            }
        return {"translated_text": translated, "model": settings.groq_model, "cost": cost, **usage}
    except Exception as exc:  # noqa: BLE001
        return {
            "translated_text": None,
            "model": f"fallback (translation failed: {exc}; showing English)",
            "cost": 0.0,
            **ZERO_TOKENS,
        }
