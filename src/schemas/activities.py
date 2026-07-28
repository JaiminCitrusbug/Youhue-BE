"""FR-14-02 — POST /api/v1/activities/{id}/run shapes."""
import uuid

from pydantic import BaseModel, ConfigDict


class ActivityRunRequest(BaseModel):
    """`target` is `class:{id}` (run with the whole class) or `student:{id}` (assign to one student
    in the caller's own class) — the ticket's own literal syntax, not free-form."""

    model_config = ConfigDict(extra="forbid")

    target: str


class ActivityRunResponse(BaseModel):
    assigned: list[uuid.UUID]
