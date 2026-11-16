# Registering real people: officers (Team page, admins only), villages and farmer
# households (Farmers page). This replaces the made-up seed data: the app starts with
# no farmers, and every household, phone number and PIN here was entered by a signed-in
# officer. Village coordinates come from Open-Meteo's geocoding search (GeoNames), so
# weather and soil moisture are looked up for the real place.
from __future__ import annotations

import re
from datetime import date

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from backend.core.config import settings
from backend.core.database import session_scope
from backend.core.phones import is_valid_phone, normalize_phone
from backend.db.models import (
    Contact,
    District,
    FarmerPin,
    Household,
    Officer,
    OfficerAccount,
    OfficerPin,
    Plot,
    PlotHistory,
    Village,
)
from backend.officer.auth import require_admin, require_officer, require_officer_page
from backend.officer.pins import hash_password, hash_pin, is_valid_pin, MIN_PASSWORD_LENGTH


class PeopleError(ValueError):
    """A registration problem with a message safe to show to the officer."""


def _phone_taken(session: Session, phone: str) -> bool:
    normalised = normalize_phone(phone)
    if session.exec(select(OfficerAccount).where(OfficerAccount.phone == normalised)).first():
        return True
    return any(normalize_phone(c.phone) == normalised for c in session.exec(select(Contact)).all())


def create_officer_account(
    session: Session, *, district_id: int, name: str, phone: str, password: str, is_admin: bool = False
) -> Officer:
    name = " ".join((name or "").split())
    if not name:
        raise PeopleError("Enter the officer's full name.")
    if not is_valid_phone(phone):
        raise PeopleError("Enter a Uganda mobile number, like 0772 123 456.")
    if _phone_taken(session, phone):
        raise PeopleError("That phone number is already registered.")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise PeopleError(f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    officer = Officer(district_id=district_id, name=name, role="extension officer")
    session.add(officer)
    session.commit()
    session.refresh(officer)
    session.add(
        OfficerAccount(
            officer_id=officer.id,
            phone=normalize_phone(phone),
            password_hash=hash_password(password),
            is_admin=is_admin,
        )
    )
    session.commit()
    return officer


def create_village(session: Session, *, district_id: int, name: str, latitude: float, longitude: float) -> Village:
    name = " ".join((name or "").split())
    if not name:
        raise PeopleError("Enter the village name.")
    # Karamoja and the rest of Uganda sit inside this box; it catches swapped or
    # mistyped coordinates, which would fetch weather for the wrong place.
    if not (-1.6 <= latitude <= 4.3 and 29.5 <= longitude <= 35.1):
        raise PeopleError("Those coordinates are not in Uganda. Use Find to look the village up.")
    village = Village(district_id=district_id, name=name, latitude=round(latitude, 4), longitude=round(longitude, 4))
    session.add(village)
    session.commit()
    session.refresh(village)
    return village


def _next_household_code(session: Session) -> str:
    numbers = [int(m.group(1)) for h in session.exec(select(Household)).all() if (m := re.fullmatch(r"H-(\d+)", h.code))]
    return f"H-{(max(numbers) + 1) if numbers else 1:03d}"


def register_farmer(
    session: Session,
    *,
    village_id: int,
    group_id: int,
    name: str,
    phone: str,
    pin: str,
    consent: bool,
    plot_size_acres: float | None = None,
    crop: str | None = None,
    planted_on: date | None = None,
    season: str | None = None,
) -> Household:
    if not consent:
        raise PeopleError("The farmer must agree to have their farm data kept before they can be registered.")
    if session.get(Village, village_id) is None:
        raise PeopleError("Choose a village.")
    if group_id < 1:
        raise PeopleError("The group number must be 1 or more.")
    name = " ".join((name or "").split())
    if not name:
        raise PeopleError("Enter the farmer's name.")
    if not is_valid_phone(phone):
        raise PeopleError("Enter a Uganda mobile number, like 0772 123 456.")
    if _phone_taken(session, phone):
        raise PeopleError("That phone number is already registered.")
    if not is_valid_pin(pin or ""):
        raise PeopleError("The PIN must be exactly 4 digits.")
    if plot_size_acres is not None and not (0 < plot_size_acres <= 500):
        raise PeopleError("Plot size must be between 0 and 500 acres, or left blank.")

    household = Household(
        village_id=village_id, group_id=group_id, code=_next_household_code(session),
        consent_given=True, consent_date=date.today(),
    )
    session.add(household)
    session.commit()
    session.refresh(household)
    session.add(Contact(household_id=household.id, name=name, phone=normalize_phone(phone)))
    session.add(FarmerPin(household_id=household.id, pin_hash=hash_pin(pin)))
    plot = Plot(household_id=household.id, size_acres=plot_size_acres)
    session.add(plot)
    session.commit()
    session.refresh(plot)
    crop = (crop or "").strip().lower() or None
    if crop:
        session.add(
            PlotHistory(
                plot_id=plot.id,
                season=season or f"{date.today().year} {'first' if date.today().month <= 7 else 'second'} season",
                crop=crop,
                planted_on=planted_on,
            )
        )
    session.commit()
    session.refresh(household)
    return household


def set_farmer_pin(session: Session, household_id: int, pin: str) -> None:
    if not is_valid_pin(pin or ""):
        raise PeopleError("The PIN must be exactly 4 digits.")
    if session.get(Household, household_id) is None:
        raise PeopleError("No such household.")
    row = session.get(FarmerPin, household_id) or FarmerPin(household_id=household_id, pin_hash="")
    row.pin_hash = hash_pin(pin)
    row.failed_attempts = 0
    row.locked_until = None
    session.add(row)
    session.commit()


def geocode(query: str) -> list[dict]:
    """Places in Uganda matching `query`, from Open-Meteo's geocoding API."""
    query = (query or "").strip()
    if len(query) < 2:
        return []
    try:
        response = httpx.get(
            settings.open_meteo_geocoding_url,
            params={"name": query, "count": 10, "language": "en", "format": "json", "countryCode": "UG"},
            timeout=8.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise PeopleError("The place search is not reachable right now. Type the coordinates instead.") from exc
    return [
        {
            "name": r.get("name"),
            "area": ", ".join(x for x in (r.get("admin2"), r.get("admin1")) if x),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
        }
        for r in response.json().get("results", []) or []
        if r.get("country_code") == "UG"
    ]


# ---------------------------------------------------------------------------
# JSON API (officers only; adding officers is admin only)
# ---------------------------------------------------------------------------
api = APIRouter(prefix="/api", tags=["people"], dependencies=[Depends(require_officer)])


def _district_id(session: Session, officer: dict) -> int:
    row = session.get(Officer, officer["officer_id"])
    if row is None:
        raise HTTPException(403, "Officer not found.")
    return row.district_id


class VillageBody(BaseModel):
    name: str
    latitude: float
    longitude: float


class FarmerBody(BaseModel):
    village_id: int
    group_id: int = Field(ge=1)
    name: str
    phone: str
    pin: str
    consent: bool
    plot_size_acres: float | None = None
    crop: str | None = None
    planted_on: date | None = None


class PinBody(BaseModel):
    pin: str


class OfficerBody(BaseModel):
    name: str
    phone: str
    password: str
    is_admin: bool = False


class PasswordBody(BaseModel):
    password: str


@api.get("/places/search")
def places_search(q: str):
    try:
        return geocode(q)
    except PeopleError as exc:
        raise HTTPException(503, str(exc)) from exc


@api.get("/villages")
def list_villages():
    with session_scope() as session:
        return [v.model_dump() for v in session.exec(select(Village).order_by(Village.name)).all()]


@api.post("/villages")
def add_village(body: VillageBody, officer: dict = Depends(require_officer)):
    with session_scope() as session:
        try:
            v = create_village(
                session, district_id=_district_id(session, officer), name=body.name,
                latitude=body.latitude, longitude=body.longitude,
            )
        except PeopleError as exc:
            raise HTTPException(400, str(exc)) from exc
        return v.model_dump()


@api.get("/farmers")
def list_farmers():
    with session_scope() as session:
        villages = {v.id: v.name for v in session.exec(select(Village)).all()}
        has_pin = {p.household_id for p in session.exec(select(FarmerPin)).all()}
        rows = session.exec(select(Household, Contact).join(Contact, Contact.household_id == Household.id).order_by(Household.code)).all()
        out = []
        for h, c in rows:
            plot = session.exec(select(Plot).where(Plot.household_id == h.id)).first()
            history = (
                session.exec(select(PlotHistory).where(PlotHistory.plot_id == plot.id).order_by(PlotHistory.id.desc())).first()
                if plot else None
            )
            out.append({
                "household_id": h.id, "code": h.code, "group_id": h.group_id,
                "village": villages.get(h.village_id), "name": c.name, "phone": c.phone,
                "has_pin": h.id in has_pin, "consent_date": h.consent_date.isoformat() if h.consent_date else None,
                "plot_size_acres": plot.size_acres if plot else None,
                "crop": history.crop if history else None,
                "planted_on": history.planted_on.isoformat() if history and history.planted_on else None,
            })
        return out


@api.post("/farmers")
def add_farmer(body: FarmerBody):
    with session_scope() as session:
        try:
            h = register_farmer(
                session, village_id=body.village_id, group_id=body.group_id, name=body.name, phone=body.phone,
                pin=body.pin, consent=body.consent, plot_size_acres=body.plot_size_acres, crop=body.crop,
                planted_on=body.planted_on,
            )
        except PeopleError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"household_id": h.id, "code": h.code, "group_id": h.group_id}


@api.post("/farmers/{household_id}/pin")
def reset_farmer_pin(household_id: int, body: PinBody):
    with session_scope() as session:
        try:
            set_farmer_pin(session, household_id, body.pin)
        except PeopleError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"status": "pin_set"}


@api.get("/team")
def list_team():
    with session_scope() as session:
        accounts = {a.officer_id: a for a in session.exec(select(OfficerAccount)).all()}
        return [
            {
                "officer_id": o.id, "name": o.name, "role": o.role,
                "phone": accounts[o.id].phone if o.id in accounts else None,
                "is_admin": bool(o.id in accounts and accounts[o.id].is_admin),
                "can_sign_in": o.id in accounts,
            }
            for o in session.exec(select(Officer).order_by(Officer.name)).all()
        ]


@api.post("/team", dependencies=[Depends(require_admin)])
def add_officer(body: OfficerBody, officer: dict = Depends(require_officer)):
    with session_scope() as session:
        try:
            o = create_officer_account(
                session, district_id=_district_id(session, officer), name=body.name, phone=body.phone,
                password=body.password, is_admin=body.is_admin,
            )
        except PeopleError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"officer_id": o.id, "name": o.name}


@api.post("/team/me/ussd-pin")
def set_my_ussd_pin(body: PinBody, officer: dict = Depends(require_officer)):
    """The signed-in officer sets their own PIN for the officer USSD menu."""
    if not is_valid_pin(body.pin or ""):
        raise HTTPException(400, "The PIN must be exactly 4 digits.")
    with session_scope() as session:
        row = session.get(OfficerPin, officer["officer_id"]) or OfficerPin(officer_id=officer["officer_id"], pin_hash="")
        row.pin_hash = hash_pin(body.pin)
        row.failed_attempts = 0
        row.locked_until = None
        session.add(row)
        session.commit()
    return {"status": "pin_set"}


@api.post("/team/{officer_id}/password", dependencies=[Depends(require_admin)])
def reset_officer_password(officer_id: int, body: PasswordBody):
    if len(body.password or "") < MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"The password must be at least {MIN_PASSWORD_LENGTH} characters.")
    with session_scope() as session:
        account = session.get(OfficerAccount, officer_id)
        if account is None:
            raise HTTPException(404, "That officer has no sign-in account.")
        account.password_hash = hash_password(body.password)
        account.failed_attempts = 0
        account.locked_until = None
        session.add(account)
        session.commit()
    return {"status": "password_set"}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def page_router(templates) -> APIRouter:
    router = APIRouter(tags=["people-pages"], dependencies=[Depends(require_officer_page)])

    @router.get("/farmers")
    def farmers_page(request: Request):
        with session_scope() as session:
            district = session.exec(select(District)).first()
        return templates.TemplateResponse(request, "farmers.html", {"district": district})

    @router.get("/team")
    def team_page(request: Request):
        return templates.TemplateResponse(
            request, "team.html", {"officer_service_code": settings.ussd_officer_service_code}
        )

    return router
