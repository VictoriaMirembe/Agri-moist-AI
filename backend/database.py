"""
Data Access and Storage Layer for AgriMoist AI (Track C: Farmer Channels)

Connects to existing workspace datasets (Data folder/crop_calendar.csv)
and provides structured in-memory repositories for reports and visit requests,
architected for plug-and-play migration to Supabase or PostgreSQL.
"""

import csv
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any

from backend.schemas import (
    ReportCreate,
    ReportResponse,
    VisitRequestCreate,
    VisitRequestResponse,
    CropCalendarItem,
)

logger = logging.getLogger("agrimoist.database")

# Workspace root path calculation
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_FOLDER = BASE_DIR / "Data folder"
CSV_PATH = DATA_FOLDER / "crop_calendar.csv"


# ============================================================================
# In-Memory Database Store (Pre-seeded with realistic Karamoja records)
# ============================================================================

REPORTS_DB: List[Dict[str, Any]] = [
    {
        "id": "REP-KRA-001",
        "farmer_phone": "0772 890 123",
        "crop_type": "Maize",
        "issue_type": "Fall Armyworm / Insect Pests",
        "urgency": "Medium",
        "description": "Found small green caterpillars feeding in whorl centers of young maize seedlings.",
        "location": "Moroto District (Nadunget Parish)",
        "has_photo": True,
        "status": "Reviewed",
        "officer_notes": "Apply neem extract or wood ash mixed with fine sand in funnel centers. Re-inspect in 48 hours.",
        "created_at": datetime.utcnow(),
    },
    {
        "id": "REP-KRA-002",
        "farmer_phone": "0772 890 123",
        "crop_type": "Sorghum",
        "issue_type": "Soil Moisture Deficit / Scorched",
        "urgency": "Low",
        "description": "Topsoil dry after initial emergence in plot 2.",
        "location": "Moroto District (Nadunget Parish)",
        "has_photo": False,
        "status": "Resolved",
        "officer_notes": "Light rainfall arrived; soil moisture returned to 68%.",
        "created_at": datetime.utcnow(),
    }
]

VISIT_REQUESTS_DB: List[Dict[str, Any]] = [
    {
        "id": "VST-KRA-001",
        "farmer_phone": "0772 890 123",
        "reason": "Soil Moisture & Seedbed Verification",
        "preferred_time": "Next Available Morning (08:00 - 11:00)",
        "landmark": "Nadunget Parish, 300m behind Church borehole",
        "district": "Moroto District (Nadunget Parish)",
        "sms_notification": True,
        "assigned_officer": "Peter Lokol",
        "status": "Pending Itinerary",
        "created_at": datetime.utcnow(),
    }
]


# ============================================================================
# Repository Functions
# ============================================================================

def create_report(payload: ReportCreate) -> ReportResponse:
    """Stores a new pest/crop issue report."""
    report_id = f"REP-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "id": report_id,
        "farmer_phone": payload.farmer_phone,
        "crop_type": payload.crop_type,
        "issue_type": payload.issue_type,
        "urgency": payload.urgency.value if hasattr(payload.urgency, "value") else str(payload.urgency),
        "description": payload.description,
        "location": payload.location,
        "has_photo": payload.has_photo,
        "status": "Pending Review",
        "officer_notes": None,
        "created_at": datetime.utcnow(),
    }
    REPORTS_DB.insert(0, record)
    logger.info(f"Recorded issue report {report_id} for farmer {payload.farmer_phone}")
    return ReportResponse(**record)


def list_reports(farmer_phone: Optional[str] = None) -> List[ReportResponse]:
    """Retrieves all reports or filters by farmer phone number."""
    if farmer_phone:
        filtered = [r for r in REPORTS_DB if r["farmer_phone"] == farmer_phone]
        return [ReportResponse(**r) for r in filtered]
    return [ReportResponse(**r) for r in REPORTS_DB]


def create_visit_request(payload: VisitRequestCreate) -> VisitRequestResponse:
    """Stores a new extension officer visit request."""
    visit_id = f"VST-{uuid.uuid4().hex[:6].upper()}"
    record = {
        "id": visit_id,
        "farmer_phone": payload.farmer_phone,
        "reason": payload.reason,
        "preferred_time": payload.preferred_time,
        "landmark": payload.landmark,
        "district": payload.district,
        "sms_notification": payload.sms_notification,
        "assigned_officer": "Peter Lokol",
        "status": "Pending Itinerary",
        "created_at": datetime.utcnow(),
    }
    VISIT_REQUESTS_DB.insert(0, record)
    logger.info(f"Recorded visit request {visit_id} for farmer {payload.farmer_phone}")
    return VisitRequestResponse(**record)


def list_visit_requests(farmer_phone: Optional[str] = None) -> List[VisitRequestResponse]:
    """Retrieves all visit requests or filters by farmer phone number."""
    if farmer_phone:
        filtered = [v for v in VISIT_REQUESTS_DB if v["farmer_phone"] == farmer_phone]
        return [VisitRequestResponse(**v) for v in filtered]
    return [VisitRequestResponse(**v) for v in VISIT_REQUESTS_DB]


# ============================================================================
# Connects to Workspace Dataset: Data folder/crop_calendar.csv
# ============================================================================

def load_crop_calendar() -> List[CropCalendarItem]:
    """
    Parses and returns the crop calendar records from Data folder/crop_calendar.csv.
    Provides safe fallback data if the file is moved or inaccessible.
    """
    items: List[CropCalendarItem] = []
    
    if CSV_PATH.is_file():
        try:
            with open(CSV_PATH, mode="r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    items.append(
                        CropCalendarItem(
                            crop=row.get("crop", "").strip(),
                            region=row.get("region", "").strip(),
                            planting_window=row.get("planting_window", "").strip(),
                            maturity_days=row.get("maturity_days", "").strip(),
                            optimal_soil_moisture=row.get("optimal_soil_moisture", "").strip(),
                            source_name=row.get("source_name", "").strip(),
                            source_link=row.get("source_link", "").strip() or None,
                        )
                    )
            logger.info(f"Loaded {len(items)} crop calendar records from {CSV_PATH}")
            return items
        except Exception as e:
            logger.warning(f"Failed to read crop_calendar.csv: {e}. Using fallback defaults.")

    # Graceful fallback in case CSV path changes
    return [
        CropCalendarItem(
            crop="maize",
            region="Northern Uganda",
            planting_window="Mar-Apr (early planting) | Jul-Aug (later planting)",
            maturity_days="115-120",
            optimal_soil_moisture="moist soil at planting",
            source_name="NUTOFA SACCO LTD: Northern Uganda planting calendar and maize farming guide",
            source_link="https://nutofasaccoltd.com/guides/planting-calendar-northern-uganda",
        ),
        CropCalendarItem(
            crop="sorghum",
            region="Northern Uganda",
            planting_window="Apr-May",
            maturity_days="90-120",
            optimal_soil_moisture="sufficient moisture in the soil at planting",
            source_name="NAADS: Sorghum varieties grown in Uganda (NAROSORG-1 to 4 and SESO-1 to 3)",
            source_link="https://naads.or.ug/sorghum-varieties-grown-in-uganda/",
        ),
    ]
