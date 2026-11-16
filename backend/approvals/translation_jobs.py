# Translation off the approval path. Sunbird (and the Groq backup) can take several
# seconds per call, and a USSD gateway will not wait that long, so:
#   1. pretranslate(): when a draft reaches the review queue, translate it in the
#      background and keep the result in AdviceTranslation.
#   2. At approval, an unchanged draft uses that stored translation at once.
#   3. Edited text (or a draft approved before its translation finished) is approved
#      immediately with the English shown, and translated in the background; the
#      farmer page picks the translation up on its next refresh.
# A translation that changes a number is never used (model_client.translate_text).
from __future__ import annotations

import json
import logging
import threading

from sqlmodel import select

from backend.agent.model_client import translate_text
from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Advice, AdviceTranslation, Run, ToolLog

logger = logging.getLogger("agrimoist.translation")
_TOKEN_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")


def translator_available() -> bool:
    return settings.translator_configured or settings.model_configured


RETRY_SECONDS = 30
RETRIES = 2


def _translate_with_retries(text: str) -> dict:
    """translate_text, tried again later if no usable translation came back (Sunbird
    sometimes times out). Only in the background: nobody is waiting for it."""
    import time

    result = translate_text(text, settings.farmer_language)
    total_cost = result.get("cost", 0.0)
    for _ in range(RETRIES if settings.translate_in_background else 0):
        if result["translated_text"]:
            break
        time.sleep(RETRY_SECONDS)
        result = translate_text(text, settings.farmer_language)
        total_cost += result.get("cost", 0.0)
    return {**result, "cost": round(total_cost, 6)}


def _start(target, *args) -> None:
    if not translator_available():
        return
    if settings.translate_in_background:
        threading.Thread(target=target, args=args, daemon=True).start()
    else:  # tests: run it now, so the result can be checked straight after
        target(*args)


def stored_translation(session, advice_id: int, text: str) -> dict | None:
    """The background translation of exactly this text, if one finished."""
    row = session.get(AdviceTranslation, advice_id)
    if row is None or row.source_text != text:
        return None
    return {
        "translated_text": row.translated_text,
        "model": row.model,
        "cost": row.cost,
        **{k: getattr(row, k) for k in _TOKEN_KEYS},
    }


def pretranslate(advice_id: int) -> None:
    """Translate a waiting draft in the background (does nothing without a key)."""
    _start(_pretranslate, advice_id)


def _pretranslate(advice_id: int) -> None:
    try:
        with session_scope() as session:
            advice = session.get(Advice, advice_id)
            if advice is None or advice.status != "pending_review" or not advice.draft_text:
                return
            text = advice.draft_text
        result = _translate_with_retries(text)
        with session_scope() as session:
            row = session.get(AdviceTranslation, advice_id) or AdviceTranslation(advice_id=advice_id, source_text=text)
            row.source_text = text
            row.translated_text = result["translated_text"]
            row.model = result["model"]
            row.cost = result.get("cost", 0.0)
            for k in _TOKEN_KEYS:
                setattr(row, k, int(result.get(k) or 0))
            session.add(row)
            session.commit()
    except Exception:  # noqa: BLE001 - a failed pre-translation only means translating later
        logger.exception("pre-translation of advice %s failed", advice_id)


def translate_after_approval(advice_id: int, text: str) -> None:
    """Translate approved text in the background and store it on the advice."""
    _start(_translate_after_approval, advice_id, text)


def _translate_after_approval(advice_id: int, text: str) -> None:
    try:
        result = _translate_with_retries(text)
        with session_scope() as session:
            advice = session.get(Advice, advice_id)
            # Only if the approved text is still the text that was translated.
            if advice is None or advice.final_text != text:
                return
            advice.translated_text = result["translated_text"]
            session.add(advice)
            run = session.get(Run, advice.run_id) if advice.run_id else None
            if run is not None and result.get("cost"):
                run.cost = round((run.cost or 0.0) + result["cost"], 6)
                session.add(run)
            session.add(
                ToolLog(
                    run_id=advice.run_id,
                    tool="translate_advice",
                    inputs=json.dumps({"advice_id": advice_id, "chars": len(text)}),
                    outputs=json.dumps(
                        {
                            "translation_model": result["model"],
                            "translated": bool(result["translated_text"]),
                            "translation_cost": result.get("cost", 0.0),
                            **{f"translation_{k}": int(result.get(k) or 0) for k in _TOKEN_KEYS},
                        }
                    ),
                    source="translator",
                    actor="assistant",
                )
            )
            session.commit()
    except Exception:  # noqa: BLE001 - the English stays shown; nothing else breaks
        logger.exception("translation of approved advice %s failed", advice_id)
    prepare_voice(advice_id)


def prepare_voice(advice_id: int) -> None:
    """Make the read-aloud audio of approved advice for its farmers before they open
    the page (Sunbird needs 15-70 s per piece, too long to wait for on Listen)."""
    if not settings.translator_configured:
        return
    try:
        from backend.farmer.router import prepare_voice_for_group  # avoids an import cycle

        with session_scope() as session:
            advice = session.get(Advice, advice_id)
            group_id = advice.group_id if advice and advice.status in ("approved", "edited_and_approved") else None
        if group_id is not None:
            prepare_voice_for_group(group_id)
    except Exception:  # noqa: BLE001 - the page makes the audio itself if this fails
        logger.exception("preparing voice for advice %s failed", advice_id)


def prepare_voice_after_approval(advice_id: int) -> None:
    if settings.translator_configured and settings.translate_in_background:
        threading.Thread(target=prepare_voice, args=(advice_id,), daemon=True).start()


def pretranslate_run_drafts(run_id: int) -> None:
    """After an agent run: start translating the draft it put in the review queue."""
    if not translator_available():
        return
    with session_scope() as session:
        ids = session.exec(
            select(Advice.id).where(Advice.run_id == run_id, Advice.status == "pending_review")
        ).all()
    for advice_id in ids:
        pretranslate(advice_id)
