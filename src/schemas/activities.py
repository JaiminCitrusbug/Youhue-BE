"""FR-14-02 — GET /api/v1/activities/seed + POST /api/v1/activities/{id}/run shapes."""
import uuid

from pydantic import BaseModel, ConfigDict


class ActivityRunRequest(BaseModel):
    """`target` is `class:{id}` (run with the whole class) or `student:{id}` (assign to one student
    in the caller's own class) — the ticket's own literal syntax, not free-form."""

    model_config = ConfigDict(extra="forbid")

    target: str


class ActivityRunResponse(BaseModel):
    assigned: list[uuid.UUID]


class SeedActivityOut(BaseModel):
    id: uuid.UUID
    title: str
    type: str
    topic: str | None


class SeedActivityListResponse(BaseModel):
    """Minimal-GET-add (not in the ticket's literal DoD, same precedent as `GET /classes/mine` /
    `GET /classes/{id}/roster`, FR-02-03/FR-10-02's own docstrings): the ticket's own Scenario 1
    text ("Given a teacher is viewing the seed activity set for their class") requires a
    teacher-facing read of the active seed set that no prior ticket exposed — FR-19-04's own list
    read is admin-only. Read-only, same active-set-only shape `checkin_db.list_seed_activities`
    already returns; never school-scoped/school-authored activities (Phase 2, out of scope)."""

    activities: list[SeedActivityOut]
