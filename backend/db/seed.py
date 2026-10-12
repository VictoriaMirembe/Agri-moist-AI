# Made-up demo data for Napak, Karamoja: 10 households in 2 villages, 3 household
# groups, plots, visits and a few messy records on purpose (T12/T13). No real person's
# details. Dates are relative to "now" so "no visit in 30 days" style checks still make
# sense no matter when the demo is run.
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlmodel import Session, select

from backend.db.models import (
    Advice,
    AdviceSource,
    Approval,
    Contact,
    CropCalendar,
    District,
    Followup,
    Household,
    Observation,
    Officer,
    Plot,
    PlotHistory,
    Run,
    Village,
    VisitFlag,
)

CROP_CALENDAR_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "crop_calendar.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _days_ago(n: int) -> datetime:
    return _now() - timedelta(days=n)


def already_seeded(session: Session) -> bool:
    return session.exec(select(District)).first() is not None


def seed(session: Session) -> None:
    if already_seeded(session):
        demo_contact = session.exec(
            select(Contact).where(Contact.phone == "+256700000010")
        ).first()
        if demo_contact and demo_contact.name == "Loru James":
            demo_contact.name = "Maria Akol"
            session.add(demo_contact)
            session.commit()
        return

    district = District(name="Napak", agroclimatic_zone="Karamoja agro-pastoral")
    session.add(district)
    session.commit()
    session.refresh(district)

    lokopo = Village(district_id=district.id, name="Lokopo", latitude=2.3833, longitude=34.3333)
    iriiri = Village(district_id=district.id, name="Iriiri", latitude=2.5333, longitude=34.3500)
    session.add(lokopo)
    session.add(iriiri)
    session.commit()
    session.refresh(lokopo)
    session.refresh(iriiri)

    officer_1 = Officer(district_id=district.id, name="Among Betty", role="extension officer")
    officer_2 = Officer(district_id=district.id, name="Lomuria Peter", role="extension officer")
    session.add(officer_1)
    session.add(officer_2)
    session.commit()
    session.refresh(officer_1)
    session.refresh(officer_2)

    # (code, village, group_id, contact name, phone, plot size or None, plot history, observations)
    households_plan = [
        dict(code="H-001", village=lokopo, group_id=1, contact="Akello Susan", phone="+256700000001",
             size_acres=1.5,
             plot_history=[dict(season="2026 first season", crop="sorghum",
                                 planted_on=date(2026, 4, 2), harvested_on=None,
                                 yield_note="Even stand, no gaps seen at last visit.")],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_1,
                                 days_ago=5, crop="sorghum", soil_wetness="moist",
                                 rain_last_7_days="some", pest_seen=None,
                                 crop_condition="good", food_left="enough",
                                 notes="Crop looks healthy.")]),
        dict(code="H-002", village=lokopo, group_id=1, contact="Nakiru Immaculate", phone="+256700000002",
             size_acres=0.75,
             plot_history=[dict(season="2026 first season", crop="maize",
                                 planted_on=date(2026, 3, 20), harvested_on=None,
                                 yield_note=None)],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_1,
                                 days_ago=3, crop="maize", soil_wetness="dry",
                                 rain_last_7_days="none", pest_seen="fall armyworm",
                                 crop_condition="fair", food_left="enough",
                                 notes="Armyworm damage on a few leaves, edges of plot.")]),
        dict(code="H-003", village=lokopo, group_id=1, contact="Lokol Simon", phone="+256700000003",
             size_acres=2.0,
             plot_history=[dict(season="2025 second season", crop="sorghum",
                                 planted_on=date(2025, 9, 10), harvested_on=date(2025, 12, 20),
                                 yield_note="Below-average yield, short dry spell mid-season.")],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_2,
                                 days_ago=42, crop="sorghum", soil_wetness="dry",
                                 rain_last_7_days="none", pest_seen=None,
                                 crop_condition="fair", food_left="little",
                                 notes="No visit since; household due a check.")]),
        dict(code="H-004", village=lokopo, group_id=2, contact="Among Grace", phone="+256700000004",
             size_acres=1.2,
             plot_history=[dict(season="2026 first season", crop="maize",
                                 planted_on=date(2026, 3, 28), harvested_on=None,
                                 yield_note=None)],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_1,
                                 days_ago=2, crop="maize", soil_wetness="wet",
                                 rain_last_7_days="heavy", pest_seen=None,
                                 crop_condition="good", food_left="enough",
                                 notes="Standing water in low corner of plot after heavy rain.")]),
        dict(code="H-005", village=lokopo, group_id=2, contact="Lomuria Daniel", phone="+256700000005",
             size_acres=None,  # messy: plot size never recorded
             plot_history=[dict(season="2026 first season", crop="sorghum",
                                 planted_on=date(2026, 4, 10), harvested_on=None,
                                 yield_note=None)],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_1,
                                 days_ago=4, crop="sorghum", soil_wetness="dry",
                                 rain_last_7_days="none", pest_seen=None,
                                 crop_condition="poor", food_left="none",
                                 notes="Household reports no food stock left; young children in home.")]),
        dict(code="H-006", village=lokopo, group_id=2, contact="Napeyok Rose", phone="+256700000006",
             size_acres=1.0,
             plot_history=[dict(season="2026 first season", crop="maize",
                                 planted_on=None, harvested_on=None,  # messy: planting date missing
                                 yield_note="Officer unsure of exact planting date; farmer away at last two visits.")],
             observations=[
                 dict(source="officer_visit", checked="checked", officer=officer_2,
                      days_ago=3, crop="maize", soil_wetness="dry",
                      rain_last_7_days="some", pest_seen=None,
                      crop_condition="fair", food_left="enough",
                      notes="Soil dry on the surface at midday visit."),
                 dict(source="officer_visit", checked="checked", officer=officer_2,
                      days_ago=2, crop="maize", soil_wetness="wet",
                      rain_last_7_days="some", pest_seen=None,
                      crop_condition="fair", food_left="enough",
                      notes="Soil wet after afternoon rain; readings disagree with the visit two days ago."),
             ]),
        dict(code="H-007", village=iriiri, group_id=3, contact="Chebet Mary", phone="+256700000007",
             size_acres=1.8,
             plot_history=[dict(season="2026 first season", crop="sorghum",
                                 planted_on=date(2026, 4, 5), harvested_on=None,
                                 yield_note=None)],
             observations=[
                 dict(source="officer_visit", checked="checked", officer=officer_2,
                      days_ago=6, crop="sorghum", soil_wetness="moist",
                      rain_last_7_days="some", pest_seen=None,
                      crop_condition="good", food_left="enough",
                      notes="No issues at visit."),
                 dict(source="farmer_ussd", checked="unchecked", officer=None,
                      days_ago=1, crop="sorghum", soil_wetness=None,
                      rain_last_7_days=None, pest_seen="fall armyworm",
                      crop_condition=None, food_left=None,
                      notes="Reported by USSD: pests seen on sorghum."),
             ]),
        dict(code="H-008", village=iriiri, group_id=3, contact="Lokwang Peter", phone="+256700000008",
             size_acres=0.9,
             plot_history=[dict(season="2026 first season", crop="maize",
                                 planted_on=date(2026, 3, 15), harvested_on=None,
                                 yield_note=None)],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_2,
                                 days_ago=10, crop="maize", soil_wetness="moist",
                                 rain_last_7_days="some", pest_seen=None,
                                 crop_condition="good", food_left="enough",
                                 notes="Routine visit, no concerns.")]),
        dict(code="H-009", village=iriiri, group_id=3, contact="Nangiro Esther", phone="+256700000009",
             size_acres=1.4,
             plot_history=[dict(season="2025 second season", crop="maize",
                                 planted_on=date(2025, 9, 1), harvested_on=date(2025, 12, 5),
                                 yield_note="Harvested; field now resting.")],
             observations=[dict(source="officer_visit", checked="checked", officer=officer_1,
                                 days_ago=7, crop="maize", soil_wetness="dry",
                                 rain_last_7_days="none", pest_seen=None,
                                 crop_condition="fair", food_left="enough",
                                 notes="Farmer asking whether it is too late to plant maize this year.")]),
        dict(code="H-010", village=iriiri, group_id=3, contact="Maria Akol", phone="+256700000010",
             size_acres=2.2,
             plot_history=[dict(season="2026 first season", crop="sorghum",
                                 planted_on=date(2026, 4, 1), harvested_on=None,
                                 yield_note=None)],
             observations=[
                 dict(source="officer_visit", checked="checked", officer=officer_1,
                      days_ago=8, crop="sorghum", soil_wetness="dry",
                      rain_last_7_days="none", pest_seen=None,
                      crop_condition="fair", food_left="enough",
                      notes="Dry spell noted."),
                 dict(source="farmer_web", checked="unchecked", officer=None,
                      days_ago=1, crop="sorghum", soil_wetness=None,
                      rain_last_7_days="none", pest_seen=None,
                      crop_condition=None, food_left=None,
                      notes="Reported by web: no rain for over a week, asking for advice."),
             ]),
    ]

    for plan in households_plan:
        household = Household(
            village_id=plan["village"].id,
            group_id=plan["group_id"],
            code=plan["code"],
            consent_given=True,
            consent_date=date(2026, 10, 1),
        )
        session.add(household)
        session.commit()
        session.refresh(household)

        session.add(Contact(household_id=household.id, name=plan["contact"], phone=plan["phone"]))

        plot = Plot(household_id=household.id, size_acres=plan["size_acres"])
        session.add(plot)
        session.commit()
        session.refresh(plot)

        for ph in plan["plot_history"]:
            session.add(PlotHistory(plot_id=plot.id, **ph))

        for obs in plan["observations"]:
            officer = obs.pop("officer")
            days_ago = obs.pop("days_ago")
            session.add(
                Observation(
                    household_id=household.id,
                    recorded_by=officer.id if officer else None,
                    observed_on=_days_ago(days_ago),
                    **obs,
                )
            )

    # Crop calendar, from data/crop_calendar.json (owned by the field/data track, sourced).
    calendar_rows = json.loads(CROP_CALENDAR_FILE.read_text(encoding="utf-8"))
    for row in calendar_rows:
        session.add(
            CropCalendar(
                zone=row["zone"],
                crop=row["crop"],
                plant_from=row["plant_from"],
                plant_to=row["plant_to"],
                harvest_from=row["harvest_from"],
                harvest_to=row["harvest_to"],
                source_url=row["source_url"],
            )
        )
    session.commit()

    # One already-approved piece of advice for group 1, so the farmer "My advice"
    # screen and USSD menu have something to show straight after seeding.
    approved_run = Run(
        group_id=1,
        model="seed-data",
        started_at=_days_ago(2),
        finished_at=_days_ago(2),
        cost=0.0,
        outcome="drafted",
    )
    session.add(approved_run)
    session.commit()
    session.refresh(approved_run)

    approved_advice = Advice(
        group_id=1,
        run_id=approved_run.id,
        status="approved",
        draft_text=(
            "Water is not urgent this week: rain has been light to moderate and soil at H-001 "
            "was moist at the last visit. Watch H-002 for fall armyworm and check the plot again "
            "within a week."
        ),
        final_text=(
            "Water is not urgent this week: rain has been light to moderate and soil at H-001 "
            "was moist at the last visit. Watch H-002 for fall armyworm and check the plot again "
            "within a week."
        ),
        why_text=(
            "Soil was moist at the last recorded visit and some rain fell in the last 7 days, "
            "so watering is not urgent. Fall armyworm was seen on one plot in the group, so a "
            "pest check is advised."
        ),
        translated_text=None,
        language="English",
        created_at=_days_ago(2),
    )
    session.add(approved_advice)
    session.commit()
    session.refresh(approved_advice)

    session.add_all(
        [
            AdviceSource(
                advice_id=approved_advice.id,
                fact="Soil at H-001 was moist, some rain in the last 7 days",
                source_type="visit",
                source_ref="observation:household=H-001",
                retrieved_at=_days_ago(2),
            ),
            AdviceSource(
                advice_id=approved_advice.id,
                fact="Fall armyworm seen on H-002",
                source_type="visit",
                source_ref="observation:household=H-002",
                retrieved_at=_days_ago(2),
            ),
            AdviceSource(
                advice_id=approved_advice.id,
                fact="Sorghum planting window for this zone",
                source_type="crop_calendar",
                source_ref="https://fews.net/sites/default/files/documents/reports/Karamoja%20Final_LHZ%20Report.pdf",
                retrieved_at=_days_ago(2),
            ),
        ]
    )
    session.add(
        Approval(
            advice_id=approved_advice.id,
            officer_id=officer_1.id,
            decision="approved",
            reason=None,
            decided_at=_days_ago(2),
        )
    )
    session.add(
        Followup(
            advice_id=approved_advice.id,
            due_on=(_days_ago(-12)).date(),
            followed=None,
            helped=None,
            answered_via=None,
        )
    )

    # One draft still waiting for approval, for group 2 - used to prove that an
    # unapproved draft never reaches a farmer (EVALS task 13).
    pending_run = Run(
        group_id=2,
        model="seed-data",
        started_at=_days_ago(1),
        finished_at=_days_ago(1),
        cost=0.0,
        outcome="drafted",
    )
    session.add(pending_run)
    session.commit()
    session.refresh(pending_run)

    pending_advice = Advice(
        group_id=2,
        run_id=pending_run.id,
        status="pending_review",
        draft_text=(
            "Heavy rain fell at H-004 in the last 7 days; hold off on watering this week."
        ),
        why_text="Rain was heavy in the last 7 days at H-004, so no extra watering is needed.",
        created_at=_days_ago(1),
    )
    session.add(pending_advice)
    session.commit()
    session.refresh(pending_advice)
    session.add(
        AdviceSource(
            advice_id=pending_advice.id,
            fact="Heavy rain in the last 7 days at H-004",
            source_type="visit",
            source_ref="observation:household=H-004",
            retrieved_at=_days_ago(1),
        )
    )

    # Visit flags already open, so the officer's Visit flags screen has data on first load.
    session.add(
        VisitFlag(
            group_id=1,
            reason="No field visit to H-003 in over 30 days",
            raised_by="assistant",
            status="open",
            created_at=_days_ago(1),
        )
    )
    session.add(
        VisitFlag(
            group_id=2,
            reason="H-005 reports very little food left",
            raised_by="assistant",
            status="open",
            created_at=_days_ago(4),
        )
    )

    session.commit()
