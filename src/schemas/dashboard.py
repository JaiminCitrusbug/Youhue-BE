"""FR-10-01 — the class dashboard response. Every field is server-owned and rendered as-is by the
FE (SRS §13.5 render-not-recompute); the FE never derives `trend` from `mood_index` deltas."""
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
