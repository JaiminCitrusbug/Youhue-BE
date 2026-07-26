"""FR-02-04 — the general staff lifecycle status endpoint. Distinct from
``schemas.staff.StaffUpdateRequest`` (FR-16-02's deactivation-only write surface, unchanged) —
this accepts any NAMED state so the one shared state machine (``staff_lifecycle.transition``) is
the sole arbiter of what's actually legal (409 otherwise), not the request schema.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict

from src.schemas.staff import StaffOut

StaffStatusLiteral = Literal["invited", "sent", "accepted", "active", "deactivated"]


class StaffStatusUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: StaffStatusLiteral


class StaffStatusUpdateResponse(BaseModel):
    status: StaffStatusLiteral
    staff: StaffOut


class ErrorResponse(BaseModel):
    detail: str
