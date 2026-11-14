# Officer USSD menu: approve or reject pending drafts from a basic phone, with no
# web app. Dialled on its own service code (USSD_OFFICER_SERVICE_CODE), so it never
# mixes with the farmer menu.
#
#   officer id > 4-digit PIN > up to 3 pending drafts > 1 Approve / 2 Reject / 0 Back
#
# This module decides nothing itself. Every approve and reject goes through
# load_decidable + apply_decision in backend/approvals/router.py - the same gate
# the web uses - so expiry, "pending only" and "decide once" hold here too.
# No editing over USSD; editing stays on the web.
#
# Irrigation proposals (SIMULATED valve) are listed in the same queue, labelled
# "IRRIGATE H-009 20 min". Approving one here goes through the same gate, which then
# hands it to the approval executor - the valve opens only after that approval.
#
# The gateway sends everything typed so far on every keypress ("2*5678*1*1"), and
# resends a request it timed out on. So each request signs in again from the typed
# PIN, the PIN is never written anywhere, and a repeated request finds the draft
# already decided and changes nothing.
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import select, update

from backend.approvals.irrigation_executor import proposal_for
from backend.approvals.router import (
    EXPIRED_MESSAGE,
    GateRefusal,
    apply_decision,
    expire_stale_drafts,
    load_decidable,
)
from backend.core.config import settings
from backend.core.database import session_scope
from backend.db.models import Advice, Household, Officer, OfficerPin, UssdSession, Village
from backend.officer.pins import verify_pin

MAX_DRAFTS = 3
PREVIEW_CHARS = 100
REJECT_REASONS = {
    "1": "Facts look wrong",
    "2": "Does not fit this group",
    "3": "Wording is unclear",
    "4": "Out of date",
}
APPROVE, REJECT, BACK = "1", "2", "0"

ASK_ID = "CON AgriMoist officers\nEnter your officer id:"
ASK_PIN = "CON Enter your 4-digit PIN:"
WRONG_PIN = "END Wrong officer id or PIN."
LOCKED = "END Too many wrong PINs. Try again later."
NO_DRAFTS = "END No drafts are waiting for approval."
NOT_ON_LIST = "That choice is not on the list.\n"
STATUS_WORDS = {
    "approved": "approved",
    "edited_and_approved": "approved with edits",
    "rejected": "rejected",
    "blocked": "blocked",
}

REFUSED = "officer:refused:"  # last_screen prefix once a session's sign-in was refused


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _save_screen(session, session_id: str, phone: str, screen: str) -> None:
    """Remember where this USSD session is. The PIN is never part of `screen`."""
    row = session.get(UssdSession, session_id) or UssdSession(session_id=session_id, phone=phone)
    row.last_screen = screen
    row.updated_at = _now()
    session.add(row)
    session.commit()


def _sign_in(session, officer_id_text: str, pin: str) -> tuple[Officer | None, str]:
    """The officer for this id and PIN, or (None, the END reply). Wrong PINs are
    counted per officer; at USSD_PIN_MAX_ATTEMPTS the sign-in is locked for
    USSD_PIN_LOCK_MINUTES, and while locked even the right PIN is refused."""
    officer = session.get(Officer, int(officer_id_text)) if officer_id_text.isascii() and officer_id_text.isdigit() else None
    record = session.get(OfficerPin, officer.id) if officer is not None else None
    if officer is None or record is None:
        return None, WRONG_PIN  # same words as a wrong PIN: do not reveal which ids exist
    if record.locked_until is not None and _aware(record.locked_until) > _now():
        return None, LOCKED

    if verify_pin(pin, record.pin_hash):
        if record.failed_attempts or record.locked_until is not None:
            record.failed_attempts = 0
            record.locked_until = None
            session.add(record)
            session.commit()
        return officer, ""

    # Count in the database (not in Python), so guesses sent at the same time all count.
    session.exec(
        update(OfficerPin)
        .where(OfficerPin.officer_id == officer.id)
        .values(failed_attempts=OfficerPin.failed_attempts + 1)
    )
    session.commit()
    session.refresh(record)
    if record.failed_attempts >= settings.ussd_pin_max_attempts:
        record.failed_attempts = 0
        record.locked_until = _now() + timedelta(minutes=settings.ussd_pin_lock_minutes)
        session.add(record)
        session.commit()
        return None, LOCKED
    return None, WRONG_PIN


def _group_name(session, group_id: int) -> str:
    household = session.exec(select(Household).where(Household.group_id == group_id)).first()
    village = session.get(Village, household.village_id) if household else None
    return f"Group {group_id} {village.name}" if village else f"Group {group_id}"


def _preview(advice: Advice) -> str:
    text = " ".join((advice.draft_text or "").split())
    if text.startswith("IRRIGATE "):  # an irrigation proposal: its label is the whole line
        return text
    return text if len(text) <= PREVIEW_CHARS else text[: PREVIEW_CHARS - 3].rstrip() + "..."


def _status_line(status: str) -> str:
    if status == "expired":
        return EXPIRED_MESSAGE
    return f"Already {STATUS_WORDS.get(status, status)}: nothing changed."


def _session_drafts(session, session_id: str, phone: str, officer: Officer) -> list[int]:
    """The draft ids this session's list shows, newest first. Fixed at sign-in and
    kept for the whole session, so "1" always means the draft the officer saw as 1,
    even if a new draft arrives between two keypresses."""
    prefix = f"officer:{officer.id}:drafts:"
    row = session.get(UssdSession, session_id)
    if row is not None and row.last_screen.startswith(prefix):
        return [int(part) for part in row.last_screen[len(prefix):].split(",") if part.isdigit()]
    ids = session.exec(
        select(Advice.id)
        .where(Advice.status == "pending_review")
        .order_by(Advice.created_at.desc(), Advice.id.desc())
        .limit(MAX_DRAFTS)
    ).all()
    if ids:
        _save_screen(session, session_id, phone, prefix + ",".join(str(i) for i in ids))
    return list(ids)


def _resolve(choices: list[str], draft_count: int) -> tuple[list[str], bool]:
    """Turn the keys typed after the PIN into the path the officer is on: [draft],
    [draft, action] or [draft, action, reason]. "0" goes back a screen, and a key
    that is not on the screen is dropped so the same screen shows again (the second
    value is True when the last key was dropped). Stops at a finished decision."""
    steps: list[str] = []
    invalid = False
    for key in choices:
        key = key.strip()
        invalid = False
        if key == BACK:
            if steps:
                steps.pop()
        elif not steps and key.isdigit() and 1 <= int(key) <= draft_count:
            steps.append(key)
        elif len(steps) == 1 and key in (APPROVE, REJECT):
            steps.append(key)
        elif len(steps) == 2 and steps[1] == REJECT and key in REJECT_REASONS:
            steps.append(key)
        else:
            invalid = True
        if steps[1:] == [APPROVE] or len(steps) == 3:
            break
    return steps, invalid


def _decide(session, officer: Officer, advice_id: int, decision: str, reason: str | None) -> str:
    """Send the decision through the approval gate and word its answer for the phone."""
    try:
        advice = load_decidable(session, advice_id)
        result = apply_decision(session, advice, officer, decision, channel="ussd", reason=reason)
    except GateRefusal as refusal:
        if refusal.advice_status is None:
            return "END That draft no longer exists."
        return "END " + _status_line(refusal.advice_status)
    if result["status"] == "rejected":
        if proposal_for(session, advice_id) is not None:
            return f"END Rejected by {officer.name}: {reason}. The valve stays closed."
        return f"END Rejected by {officer.name}: {reason}. Nothing is sent to farmers."
    irrigation = result.get("irrigation")
    if irrigation is not None:
        plot = irrigation["valve"]["plot"]
        if irrigation["opened"]:
            minutes = proposal_for(session, advice_id).minutes
            return f"END Approved by {officer.name}. Valve {plot} open for {minutes} min (simulated), then it closes."
        return f"END Approved by {officer.name}, but the valve {plot} did not open and is closed. See the web app."
    return f"END Approved by {officer.name}. Farmers in {_group_name(session, advice.group_id)} can now read it."


def officer_ussd_reply(session_id: str, text: str, phone: str = "") -> str:
    typed = [part.strip() for part in text.strip().split("*")] if text.strip() else []
    if not typed:
        return ASK_ID
    if len(typed) == 1:
        return ASK_PIN

    with session_scope() as session:
        row = session.get(UssdSession, session_id)
        if row is not None and row.last_screen.startswith(REFUSED):
            # The gateway resent a refused sign-in: same answer, not a second wrong attempt.
            return LOCKED if row.last_screen == REFUSED + "locked" else WRONG_PIN
        officer, refusal = _sign_in(session, typed[0], typed[1])
        if officer is None:
            _save_screen(session, session_id, phone, REFUSED + ("locked" if refusal == LOCKED else "wrong"))
            return refusal

        expire_stale_drafts(session)
        draft_ids = _session_drafts(session, session_id, phone, officer)
        if not draft_ids:
            return NO_DRAFTS
        steps, invalid = _resolve(typed[2:], len(draft_ids))
        retry = NOT_ON_LIST if invalid else ""

        if not steps:
            lines = []
            for number, advice_id in enumerate(draft_ids, start=1):
                advice = session.get(Advice, advice_id)
                if advice is None:
                    continue
                words = _preview(advice) if advice.status == "pending_review" else _status_line(advice.status)
                if (advice.draft_text or "").startswith("IRRIGATE ") and advice.status == "pending_review":
                    lines.append(f"{number}. {words}, {_group_name(session, advice.group_id)}")
                else:
                    lines.append(f"{number}. {_group_name(session, advice.group_id)}: {words}")
            return f"CON {retry}Drafts waiting:\n" + "\n".join(lines)

        advice_id = draft_ids[int(steps[0]) - 1]
        if steps[1:] == [APPROVE]:
            return _decide(session, officer, advice_id, "approve", None)
        if len(steps) == 3:
            return _decide(session, officer, advice_id, "reject", REJECT_REASONS[steps[2]])

        advice = session.get(Advice, advice_id)
        if advice is None or advice.status != "pending_review":
            # Expired or decided since the list was shown: say so, offer no action.
            return f"CON {_status_line(advice.status) if advice else 'That draft no longer exists.'}\n0. Back"
        if len(steps) == 2:
            reasons = "\n".join(f"{key}. {words}" for key, words in REJECT_REASONS.items())
            return f"CON {retry}Why reject?\n{reasons}\n0. Back"
        return (
            f"CON {retry}{_group_name(session, advice.group_id)}: {_preview(advice)}\n"
            "1. Approve\n2. Reject\n0. Back"
        )
