"""FR-07-04 — calendar period resolution (SC-063 read side)."""
from datetime import datetime

from pydantic import BaseModel


class ErrorResponse(BaseModel):
    """403 / 404 / 422 / 500 bodies — FastAPI's plain-string ``detail``."""

    detail: str


class CalendarResolveOut(BaseModel):
    """{start, end} — both tz-aware, in the school's CONFIGURED timezone. ``end`` is the EXCLUSIVE
    boundary of the period (start of the next period), never the period's last instant, so callers
    never have to reason about a period's final millisecond."""

    start: datetime
    end: datetime
