"""FR-04-01 — daily check-in submit + mood-set read shapes."""
import uuid

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


class MoodSetOut(BaseModel):
    """The CALLER's own age-appropriate mood set (config-driven, ticket Q-3) — a list of the
    `CheckIn.mood_value` ints the caller's age band may submit."""

    values: list[int]
