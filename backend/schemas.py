"""
Pydantic Data Schemas for TerraMoist AI (Track C: Farmer Channels)
Handles validation for crop problem reports, officer visit requests, and farmer records.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, List
from pydantic import BaseModel, Field


class UrgencyLevel(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    URGENT = "Urgent"


class IssueClassification(str, Enum):
    ARMYWORM_PESTS = "Fall Armyworm / Insect Pests"
    YELLOWING_FOLIAGE = "Foliage Yellowing / Drying"
    SOIL_MOISTURE_DEFICIT = "Soil Moisture Deficit / Scorched"
    STUNTED_GROWTH = "Stunted Growth / Root Disease"
    OTHER = "Other Crop Concern"


# ============================================================================
# 1. Pest & Crop Issue Reports
# ============================================================================

class ReportCreate(BaseModel):
    """Schema for incoming farmer issue reports."""
    farmer_phone: str = Field(..., description="Farmer phone number or Farmer ID (MTN/Airtel Uganda)", example="0772 890 123")
    crop_type: str = Field(..., description="Affected crop (e.g. Maize, Sorghum, Millet)", example="Maize")
    issue_type: str = Field(..., description="Classified problem category", example="Fall Armyworm / Insect Pests")
    urgency: UrgencyLevel = Field(default=UrgencyLevel.MEDIUM, description="Farmer assessment of severity")
    description: Optional[str] = Field(default=None, description="Detailed field notes or voice transcription", example="Small caterpillars in maize whorl centers.")
    location: Optional[str] = Field(default="Moroto District (Nadunget Parish)", description="Farm parish or sub-county")
    has_photo: Optional[bool] = Field(default=False, description="Flag indicating image attachment")


class ReportResponse(ReportCreate):
    """Schema returned after a report is recorded."""
    id: str = Field(..., description="Unique issue tracking ID")
    status: str = Field(default="Pending Review", description="Workflow status (Pending Review, Reviewed, Resolved)")
    officer_notes: Optional[str] = Field(default=None, description="Feedback from assigned extension officer")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp of submission")


# ============================================================================
# 2. Extension Officer Visit Requests
# ============================================================================

class VisitRequestCreate(BaseModel):
    """Schema for requesting on-site farm visits."""
    farmer_phone: str = Field(..., description="Farmer contact number", example="0772 890 123")
    reason: str = Field(..., description="Reason for visit", example="Soil Moisture & Seedbed Verification")
    preferred_time: str = Field(..., description="Timing window preferred by farmer", example="Next Available Morning (08:00 - 11:00)")
    landmark: str = Field(..., description="Village or physical landmark near manyatta", example="Nadunget Parish, 300m behind Church borehole")
    district: Optional[str] = Field(default="Moroto District (Nadunget Parish)", description="District/Sub-county jurisdiction")
    sms_notification: Optional[bool] = Field(default=True, description="Whether to dispatch confirmation via SMS")


class VisitRequestResponse(VisitRequestCreate):
    """Schema returned after a visit is requested."""
    id: str = Field(..., description="Unique visit booking ID")
    assigned_officer: str = Field(default="Peter Lokol", description="Designated sub-county agricultural extension officer")
    status: str = Field(default="Pending Itinerary", description="Scheduling status (Pending Itinerary, Scheduled, Completed)")
    created_at: datetime = Field(default_factory=datetime.utcnow, description="UTC timestamp of request")


# ============================================================================
# 3. Crop Calendar Dataset Schema
# ============================================================================

class CropCalendarItem(BaseModel):
    """Represents a validated crop calendar record loaded from crop_calendar.csv."""
    crop: str
    region: str
    planting_window: str
    maturity_days: str
    optimal_soil_moisture: str
    source_name: str
    source_link: Optional[str] = None
