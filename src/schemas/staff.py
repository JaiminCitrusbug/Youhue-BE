"""School leadership hub — staff management (FR-16-02 / SC-057).

``StaffUpdateRequest.status`` accepts ONLY ``"deactivated"`` — this ticket's write surface is the
deactivation action the approved screen exposes (a "Deactivate" button per row), not a general
status editor. ``extra="forbid"`` for the same reason every write body in this codebase forbids
extras: no field beyond the one status value can escalate the write.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class StaffOut(BaseModel):
    """One staff-management row (SC-057)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: datetime


class StaffListResponse(BaseModel):
    staff: list[StaffOut]


class StaffUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["deactivated"]


class StaffUpdateResponse(BaseModel):
    staff: StaffOut


class ErrorResponse(BaseModel):
    """403 / 404 / 422 / 500 bodies — FastAPI's plain-string ``detail``."""

    detail: str
