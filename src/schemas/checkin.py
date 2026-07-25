"""FR-04-01 — daily check-in submit shapes. FR-04-03 — age-banded check-in config shape."""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ErrorResponse(BaseModel):
    """403 / 409 / 422 / 500 bodies — FastAPI's plain-string ``detail``."""

    detail: str


class CheckInCreate(BaseModel):
    """POST body. `mood_value` is validated against the 0..5 ladder here (pydantic); membership in
    the CALLER's own age-banded set (ticket Q-3, config-driven) is a business-rule 422 checked by
    the service, not this schema."""

    model_config = ConfigDict(extra="forbid")

    mood_value: int = Field(ge=0, le=5)
    reflection_text: str | None = None


class ActivityOfferOut(BaseModel):
    """Stub shape for FR-05-01 (post-check-in activity — a LATER, not-yet-built ticket) to
    populate. This ticket always returns `activity_offer: null`; the field exists and is typed so
    FR-05-01 can start filling it in without a response-shape migration (see ``docs/DEFERRALS.md``,
    same "field exists, populated later" class as FR-02-02's Subscription arm-not-started note)."""

    activity_id: uuid.UUID
    title: str
    type: str


class CheckInOut(BaseModel):
    checkin_id: uuid.UUID
    activity_offer: ActivityOfferOut | None = None


class CheckInConfigOut(BaseModel):
    """FR-04-03 — `GET /api/v1/check-ins/config`: the CALLER's own age-matched check-in config.
    Supersedes FR-04-01's narrower `GET /check-ins/mood-set` (same `mood_set` data, now carried
    alongside `mode`/`read_aloud` so the mood-select screen has one contract to read instead of
    two) — resolved user decision on the FR-04-03 batch clarification gate."""

    mode: Literal["simple", "rich"]
    mood_set: list[int]
    read_aloud: bool
