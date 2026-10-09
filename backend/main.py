"""
TerraMoist AI - Track C: Farmer Channels API & Web Interface
FastAPI Backend Application Entry Point

Serves the low-bandwidth mobile frontend (farmer_portal.html) via Jinja2
and provides validated REST endpoints for farmer issue reporting,
extension visit requests, and crop advisory data.
"""

from pathlib import Path
from typing import Optional, List

from fastapi import FastAPI, Request, status, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from backend.schemas import (
    ReportCreate,
    ReportResponse,
    VisitRequestCreate,
    VisitRequestResponse,
    CropCalendarItem,
)
from backend.database import (
    create_report,
    list_reports,
    create_visit_request,
    list_visit_requests,
    load_crop_calendar,
)

# ============================================================================
# 1. Application Initialization & Path Configuration
# ============================================================================

app = FastAPI(
    title="TerraMoist AI - Track C: Farmer Channels",
    description="Low-bandwidth mobile farmer advisory portal & extension officer dispatch system for Karamoja, Uganda.",
    version="1.0.0",
)

# Resolve path to templates directory safely (relative to project root)
BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# ============================================================================
# 2. CORS Middleware Configuration (Smooth Local & Mobile Dev)
# ============================================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# 3. Frontend Web Views (Jinja2 Template Rendering)
# ============================================================================

@app.get("/", response_class=HTMLResponse, summary="Farmer Portal Home View")
@app.get("/farmer", response_class=HTMLResponse, summary="Farmer Portal Main View")
async def render_farmer_portal(
    request: Request,
    farmer_name: Optional[str] = Query(default="Maria Akol", description="Farmer display name"),
    phone: Optional[str] = Query(default="0772 890 123", description="Farmer contact number"),
    district: Optional[str] = Query(default="Moroto District (Nadunget Parish)", description="Sub-county location"),
):
    """
    Renders the clean, self-contained mobile web interface for farmers.
    Injects localized context including farmer information, assigned officer,
    and Northern Uganda crop calendar data loaded from crop_calendar.csv.
    """
    crop_calendar = load_crop_calendar()
    recent_reports = list_reports(farmer_phone=phone)
    recent_visits = list_visit_requests(farmer_phone=phone)

    context = {
        "request": request,
        "farmer_name": farmer_name,
        "farmer_phone": phone,
        "farmer_location": district,
        "officer_name": "Peter Lokol",
        "crop_calendar": crop_calendar,
        "recent_reports": recent_reports,
        "recent_visits": recent_visits,
        "soil_moisture": "68%",
        "rainfall_forecast": "18 mm",
    }
    return templates.TemplateResponse("farmer_portal.html", context)


# ============================================================================
# 4. API Endpoints: Issue Reporting (Track C Core Workflow)
# ============================================================================

@app.post(
    "/api/reports",
    response_model=ReportResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit Pest or Crop Problem Report",
)
async def submit_issue_report(payload: ReportCreate):
    """
    Receives farmer issue submissions (pests, armyworm, yellowing leaves, dry soil).
    Validates input using Pydantic, stores in repository, and queues for extension review.
    """
    new_report = create_report(payload)
    return new_report


@app.get(
    "/api/reports",
    response_model=List[ReportResponse],
    summary="List Submitted Issue Reports",
)
async def get_issue_reports(
    farmer_phone: Optional[str] = Query(default=None, description="Filter reports by farmer phone number")
):
    """
    Retrieves logged reports. Can be filtered by farmer phone number.
    """
    return list_reports(farmer_phone=farmer_phone)


# ============================================================================
# 5. API Endpoints: Extension Officer Visit Requests
# ============================================================================

@app.post(
    "/api/visit-requests",
    response_model=VisitRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Request Extension Officer Farm Visit",
)
async def submit_visit_request(payload: VisitRequestCreate):
    """
    Receives physical visit requests from smallholders who need in-person inspection,
    soil testing, or pest control advice in their manyatta or garden.
    """
    new_visit = create_visit_request(payload)
    return new_visit


@app.get(
    "/api/visit-requests",
    response_model=List[VisitRequestResponse],
    summary="List Extension Visit Requests",
)
async def get_visit_requests(
    farmer_phone: Optional[str] = Query(default=None, description="Filter visit requests by farmer phone number")
):
    """
    Retrieves all pending and confirmed farm visit requests.
    """
    return list_visit_requests(farmer_phone=farmer_phone)


# ============================================================================
# 6. API Endpoints: Crop Advisory & Calendar Dataset
# ============================================================================

@app.get(
    "/api/crop-calendar",
    response_model=List[CropCalendarItem],
    summary="Get Northern Uganda Crop Planting Calendar",
)
async def get_crop_calendar():
    """
    Returns verified crop calendar data dynamically read from Data folder/crop_calendar.csv
    (e.g., maize planting windows, sorghum drought-tolerant varieties, and optimal soil conditions).
    """
    return load_crop_calendar()


# ============================================================================
# 7. System Health Endpoint
# ============================================================================

@app.get("/api/health", summary="Health Check")
async def health_check():
    """
    System diagnostic and liveness check for TerraMoist AI gateway.
    """
    return {
        "status": "healthy",
        "system": "TerraMoist AI",
        "track": "Track C - Farmer Channels",
        "region": "Karamoja, Uganda",
        "templates_found": (TEMPLATES_DIR / "farmer_portal.html").is_file(),
    }
