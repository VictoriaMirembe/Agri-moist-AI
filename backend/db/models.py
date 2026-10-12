# Database tables, matching DATA_SCHEMA.md. Every table reaches a district, directly or
# through its village, so adding a second district later needs no redesign.
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class District(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    agroclimatic_zone: str


class Village(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    district_id: int = Field(foreign_key="district.id")
    name: str
    latitude: float
    longitude: float


class Household(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    village_id: int = Field(foreign_key="village.id")
    group_id: int
    code: str  # short code shown on screens, e.g. "H-014". No name here.
    consent_given: bool = False
    consent_date: Optional[date] = None


class Contact(SQLModel, table=True):
    """Kept apart from farm data on purpose."""

    id: Optional[int] = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    name: str
    phone: str


class Officer(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    district_id: int = Field(foreign_key="district.id")
    name: str
    role: str = "extension officer"
    login_username: Optional[str] = None
    password_hash: Optional[str] = None


class Plot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    size_acres: Optional[float] = None  # may be blank (messy data)


class PlotHistory(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plot_id: int = Field(foreign_key="plot.id")
    season: str
    crop: str
    planted_on: Optional[date] = None  # may be blank
    harvested_on: Optional[date] = None  # may be blank
    yield_note: Optional[str] = None


class Observation(SQLModel, table=True):
    """Officer visits and farmer reports, told apart by `source`."""

    id: Optional[int] = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    source: str  # officer_visit, farmer_ussd, farmer_web
    checked: str = "unchecked"  # checked for visits; unchecked for farmer reports
    recorded_by: Optional[int] = Field(default=None, foreign_key="officer.id")
    observed_on: datetime = Field(default_factory=utcnow)
    crop: Optional[str] = None
    soil_wetness: Optional[str] = None  # dry, moist, wet
    rain_last_7_days: Optional[str] = None  # none, some, heavy
    pest_seen: Optional[str] = None
    crop_condition: Optional[str] = None  # good, fair, poor
    food_left: Optional[str] = None  # enough, little, none
    notes: Optional[str] = None


class CropCalendar(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    zone: str
    crop: str
    plant_from: str  # "MM-DD"
    plant_to: str
    harvest_from: str
    harvest_to: str
    source_url: str  # required; no row without a source


class Advice(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int
    run_id: Optional[int] = Field(default=None, foreign_key="run.id")
    status: str = "drafted"  # see DESIGN.md approval states
    draft_text: Optional[str] = None
    final_text: Optional[str] = None
    why_text: Optional[str] = None
    translated_text: Optional[str] = None
    language: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class AdviceSource(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    advice_id: int = Field(foreign_key="advice.id")
    fact: str  # the claim in the advice
    source_type: str  # weather, plot_record, visit, farmer_report, crop_calendar, pest_report
    source_ref: str  # URL or record id
    retrieved_at: datetime = Field(default_factory=utcnow)


class Approval(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    advice_id: int = Field(foreign_key="advice.id")
    officer_id: int = Field(foreign_key="officer.id")
    decision: str  # approved, edited_and_approved, rejected
    reason: Optional[str] = None  # required for a rejection
    decided_at: datetime = Field(default_factory=utcnow)


class VisitFlag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int
    reason: str
    raised_by: str  # assistant, farmer_ussd, farmer_web
    status: str = "open"  # open, visited, closed
    created_at: datetime = Field(default_factory=utcnow)


class Followup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    advice_id: int = Field(foreign_key="advice.id")
    due_on: date
    followed: Optional[str] = None  # yes, no, partly
    helped: Optional[str] = None  # yes, no
    answered_via: Optional[str] = None  # officer_visit, farmer_ussd, farmer_web


class Run(SQLModel, table=True):
    """One agent run (one draft-advice attempt) for one household group."""

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: int
    model: str
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: Optional[datetime] = None
    cost: float = 0.0  # from the model provider's usage numbers
    outcome: Optional[str] = None  # drafted, flagged, failed


class ToolLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: Optional[int] = Field(default=None, foreign_key="run.id")
    tool: str
    inputs: str  # JSON
    outputs: str  # JSON
    source: Optional[str] = None
    actor: str  # assistant, an officer id, or farmer
    at: datetime = Field(default_factory=utcnow)
    error: Optional[str] = None  # blank if it worked


class UssdSession(SQLModel, table=True):
    session_id: str = Field(primary_key=True)  # from the gateway, or the simulator
    phone: str
    last_screen: str = "home"
    updated_at: datetime = Field(default_factory=utcnow)
