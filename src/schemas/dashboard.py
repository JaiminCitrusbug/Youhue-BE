"""FR-10-01 — the class dashboard response. Every field is server-owned and rendered as-is by the
FE (SRS §13.5 render-not-recompute); the FE never derives `trend` from `mood_index` deltas."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class ClassDashboardOut(BaseModel):
    class_id: str
    class_name: str
    mood_index: float | None  # None = no check-ins in-window yet — never a fabricated 0
    trend: Literal["up", "down", "flat"]
    as_of: datetime
    live: bool
    period: str
    timezone: str
    # FR-10-05: distinguishes "this class has never checked in" from "this filter matched nothing
    # even though the class has data elsewhere" — the two must never share copy (ticket §Must-nots).
    # has_data      -> mood_index is populated (real check-ins in the current window).
    # no_data_yet   -> the class has NEVER had a check-in, in any period.
    # no_results    -> the current window is empty, but the class DOES have check-ins outside it.
    data_state: Literal["has_data", "no_data_yet", "no_results"]


class ClassRosterRow(BaseModel):
    id: uuid.UUID
    display_name: str


class ClassRosterOut(BaseModel):
    """FR-10-02: the class's real student rows — what the dashboard's "click into a single
    student" (Scenario 1) links against."""

    students: list[ClassRosterRow]
