# Farmer languages: Karamojong, Luganda, Runyankole and Swahili.
#
# Every English text a farmer sees or hears (approved advice, and the farmer page's
# own sentences) is translated once per language and stored (TextTranslation), so
# the page shows it at once from then on. Numbers and names are swapped for
# placeholders first ("Welcome to your farm, [A]."), so one stored translation
# serves every farmer and every day's rain figures.
#
# Only Sunbird translates into these languages. The Groq model was tried as a fast
# backup on 2026-11-17 and its Luganda and Runyankole were often wrong (it called
# rain "lake"), and wrong advice is worse than English for a minute. Sunbird can take
# a minute per call when busy, so the slow work is done ahead of time: advice is
# translated as soon as its draft reaches the review queue, on its own queue ahead
# of the page's sentences, with few calls at a time so Sunbird is not overloaded.
# A translation that drops or changes a placeholder is never used.
from __future__ import annotations

import hashlib
import logging
import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from backend.agent import model_client
from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import TextTranslation

logger = logging.getLogger("agrimoist.translations")

# lang -> (name shown to farmers, Sunbird language code)
LANGUAGES = {
    "kdj": ("Ngakarimojong", "kdj"),
    "lug": ("Luganda", "lug"),
    "nyn": ("Runyankole", "nyn"),
    "sw": ("Kiswahili", "swa"),
}
_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="translate-page")
_ADVICE_POOL = ThreadPoolExecutor(max_workers=3, thread_name_prefix="translate-advice")
_JOBS: dict[tuple[str, str, bool], Future] = {}
_JOBS_GUARD = threading.Lock()
_NUMBER = re.compile(r"\b[A-Z]-\d+\b|\d+(?:\.\d+)?(?:\s?(?:mm|%))?")
_SLOT = re.compile(r"\[[A-Z]\d*\]")


def available() -> bool:
    return settings.translator_configured


def _slot(n: int) -> str:
    return f"[{chr(65 + n % 26)}{n // 26 or ''}]"


def protect(text: str, keep: tuple[str, ...] = ()) -> tuple[str, dict[str, str]]:
    """Swap the names in keep, then every number and plot code, for [A], [B]..."""
    slots: dict[str, str] = {}
    for value in sorted({k for k in keep if k and k.strip()}, key=len, reverse=True):
        if value in text:
            key = _slot(len(slots))
            slots[key] = value
            text = text.replace(value, key)

    def swap(match: re.Match) -> str:
        key = _slot(len(slots))
        slots[key] = match.group(0)
        return key

    return _NUMBER.sub(swap, text), slots


def _key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _usable(source: str, translated: str | None) -> bool:
    """Every placeholder comes back exactly once, and no new number appears."""
    if not translated or translated.strip() == source.strip():
        return False
    if sorted(_SLOT.findall(translated)) != sorted(_SLOT.findall(source)):
        return False
    return not re.search(r"\d", _SLOT.sub("", translated))


def _stored(lang: str, protected: str) -> TextTranslation | None:
    with session_scope() as session:
        row = session.get(TextTranslation, (lang, _key(protected)))
        if row is not None:
            session.expunge(row)
        return row


def _store(lang: str, protected: str, translated: str, model: str) -> None:
    with session_scope() as session:
        row = session.get(TextTranslation, (lang, _key(protected)))
        if row is None:
            row = TextTranslation(lang=lang, source_hash=_key(protected), source_text=protected,
                                  translated_text=translated, model=model)
        elif row.model.startswith("sunbird") and not model.startswith("sunbird"):
            return  # never replace Sunbird's translation with the backup's
        else:
            row.translated_text, row.model = translated, model
        session.add(row)
        session.commit()


def _sunbird(protected: str, lang: str) -> str | None:
    result = model_client._translate_with_sunbird(protected, LANGUAGES[lang][1])
    text = (result or {}).get("translated_text")
    return text if _usable(protected, text) else None


def _translate(protected: str, lang: str) -> bool:
    """Translate and store one placeholder text (two tries). True once stored."""
    for _ in range(2):
        text = _sunbird(protected, lang)
        if text:
            _store(lang, protected, text, "sunbird/sunflower-9b")
            return True
    return False


def ensure(text: str, lang: str, keep: tuple[str, ...] = (), priority: bool = False) -> Future | None:
    """Start translating text into lang unless it is stored or already under way.
    priority: advice, on its own queue ahead of the page's sentences."""
    if lang not in LANGUAGES or not text or not available():
        return None
    protected, _ = protect(text, keep)
    if _stored(lang, protected) is not None:
        return None
    # Keyed by queue too: advice asked for while the same words wait on the page
    # queue still goes straight onto the advice queue.
    key = (lang, protected, priority)
    with _JOBS_GUARD:
        job = _JOBS.get(key) or (_JOBS.get((lang, protected, True)) if not priority else None)
        if job is None or job.done():
            job = (_ADVICE_POOL if priority else _POOL).submit(_translate, protected, lang)
            _JOBS[key] = job
            job.add_done_callback(lambda done, k=key: _JOBS.pop(k, None))
        return job


def lookup(text: str, lang: str, keep: tuple[str, ...] = (), fill: dict[str, str] | None = None,
           priority: bool = False) -> str | None:
    """The stored translation of text with its numbers and names put back, or None
    (and translating starts in the background). fill replaces a kept value with
    another text, e.g. the English advice with its own translation."""
    if lang not in LANGUAGES or not text:
        return None
    protected, slots = protect(text, keep)
    row = _stored(lang, protected)
    if row is None:
        ensure(text, lang, keep, priority)
        return None
    out = row.translated_text
    for key, value in slots.items():
        out = out.replace(key, (fill or {}).get(value, value))
    return out


def translate_now(text: str, lang: str, keep: tuple[str, ...] = (), wait: float = 0.0) -> str | None:
    """lookup(), waiting up to wait seconds for a translation that is not stored yet."""
    found = lookup(text, lang, keep)
    if found is not None or wait <= 0:
        return found
    job = ensure(text, lang, keep)
    if job is not None:
        try:
            job.result(timeout=wait)
        except Exception:  # noqa: BLE001
            return None
    return lookup(text, lang, keep)


def ensure_all_languages(text: str, keep: tuple[str, ...] = ()) -> None:
    """Advice: start it in every farmer language (Karamojong has its own path)."""
    for lang in LANGUAGES:
        if lang != "kdj":
            ensure(text, lang, keep, priority=True)
