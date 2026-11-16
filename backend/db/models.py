# Database tables. Every table reaches a district, directly or
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


class OfficerPin(SQLModel, table=True):
    """An officer's USSD PIN, kept only as a salted scrypt hash (see
    backend/officer/pins.py), with the wrong-PIN counter. A new table so the
    existing officer columns stay exactly as they were."""

    officer_id: int = Field(primary_key=True, foreign_key="officer.id")
    pin_hash: str
    failed_attempts: int = 0
    locked_until: Optional[datetime] = None


class OfficerAccount(SQLModel, table=True):
    """An officer's web sign-in: their own phone number and password (salted scrypt
    hash only, backend/officer/pins.py), with the wrong-password counter. An admin can
    add other officers. The first account is made on the one-time /officer/setup page."""

    officer_id: int = Field(primary_key=True, foreign_key="officer.id")
    phone: str = Field(index=True, unique=True)  # stored normalised, e.g. 256772123456
    password_hash: str
    is_admin: bool = False
    failed_attempts: int = 0
    locked_until: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utcnow)


class FarmerPin(SQLModel, table=True):
    """A farmer's 4-digit sign-in PIN for the farmer web page, set by the officer who
    registers them. Hash only, with the wrong-PIN counter."""

    household_id: int = Field(primary_key=True, foreign_key="household.id")
    pin_hash: str
    failed_attempts: int = 0
    locked_until: Optional[datetime] = None


class Plot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    household_id: int = Field(foreign_key="household.id")
    size_acres: Optional[float] = None  # may be blank (messy data)


class SoilReading(SQLModel, table=True):
    """A soil-moisture reading for a plot. There is no real sensor hardware: every
    reading so far comes from a SIMULATED sensor (source "simulated sensor"), and the
    seeded ones are also marked demo=True. Read by the get_soil_moisture tool."""

    id: Optional[int] = Field(default=None, primary_key=True)
    plot_id: int = Field(foreign_key="plot.id", index=True)
    moisture_pct: float  # volumetric water content, %
    read_at: datetime = Field(default_factory=utcnow)
    source: str = "simulated sensor"
    demo: bool = False  # True for the made-up readings loaded by backend/db/seed.py


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
    status: str = "drafted"  # see docs/API_CONTRACT.md approval states
    draft_text: Optional[str] = None
    final_text: Optional[str] = None
    why_text: Optional[str] = None
    translated_text: Optional[str] = None
    language: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class AdviceTranslation(SQLModel, table=True):
    """A translation made ahead of time, in the background, as soon as a draft reaches
    the review queue. If the officer approves the text unchanged, this is used at once,
    so approving never waits for the translator. Never shown to farmers by itself:
    only Advice.translated_text on approved advice is (advice_visible_to_farmer)."""

    advice_id: int = Field(primary_key=True, foreign_key="advice.id")
    source_text: str
    translated_text: Optional[str] = None  # None: no usable translation (English is shown)
    model: str = ""
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    created_at: datetime = Field(default_factory=utcnow)


class IrrigationProposal(SQLModel, table=True):
    """A proposal to irrigate one plot (SIMULATED valve, no hardware). The decision
    itself lives on its Advice row (status pending_review -> approved / rejected /
    expired), so it goes through exactly the same approval gate as advice drafts.
    This table only holds what the proposal is about, and what the approval executor
    did after approval. A new table so the existing advice columns stay as they were."""

    id: Optional[int] = Field(default=None, primary_key=True)
    advice_id: int = Field(foreign_key="advice.id", index=True, unique=True)
    run_id: Optional[int] = Field(default=None, foreign_key="run.id")  # the agent run that proposed it
    group_id: int
    plot_id: int = Field(foreign_key="plot.id")
    plot_code: str  # the household code of the plot, e.g. "H-009"
    minutes: int  # already capped at IRRIGATION_MAX_MINUTES
    moisture_pct: float
    soil_reading_ref: str  # soil_reading:<id>
    soil_read_at: datetime
    forecast_mm: float
    weather_ref: str
    # not_started -> opening -> opened -> closed, or failed (valve left closed)
    execution: str = "not_started"
    execution_error: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)


class Valve(SQLModel, table=True):
    """The state of one plot's valve on the SIMULATED irrigation gateway
    (backend/irrigation_server). Only that server's open_valve writes 'open'."""

    plot_code: str = Field(primary_key=True)
    state: str = "closed"  # closed or open
    opened_at: Optional[datetime] = None
    close_at: Optional[datetime] = None  # when the gateway's timer closes it
    proposal_id: Optional[int] = Field(default=None, foreign_key="irrigationproposal.id")
    updated_at: datetime = Field(default_factory=utcnow)


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


class VisitFlagVisit(SQLModel, table=True):
    """Who marked a visit flag as visited, and when. A new table so the existing
    visit_flag columns stay exactly as they were."""

    id: Optional[int] = Field(default=None, primary_key=True)
    flag_id: int = Field(foreign_key="visitflag.id", index=True)
    officer_id: Optional[int] = Field(default=None, foreign_key="officer.id")  # None: not sent
    visited_at: datetime = Field(default_factory=utcnow)


class Followup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    advice_id: int = Field(foreign_key="advice.id")
    due_on: date
    followed: Optional[str] = None  # yes, no, partly
    helped: Optional[str] = None  # yes, no
    answered_via: Optional[str] = None  # officer_visit, farmer_ussd, farmer_web


class FollowupAnswer(SQLModel, table=True):
    """One household's answer to "did the advice help?". Followup above keeps the
    latest answer for the advice (unchanged behaviour); this table keeps every
    household's own answer, so answers from one group never overwrite each other.
    A household answering again updates its own row."""

    id: Optional[int] = Field(default=None, primary_key=True)
    advice_id: int = Field(foreign_key="advice.id", index=True)
    household_id: int = Field(foreign_key="household.id")
    followed: str  # yes, no, partly
    helped: Optional[str] = None  # yes, no, too_early
    answered_via: str  # farmer_ussd, farmer_web
    answered_at: datetime = Field(default_factory=utcnow)


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
