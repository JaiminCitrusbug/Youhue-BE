"""FR-14-02 — POST /api/v1/activities/{id}/run: a teacher runs a seed-set activity with their whole
class or assigns it to a specific student. Thin router — business logic lives in
src.application.activities.services."""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.activities import services as activities_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.activities import (
    ActivityRunRequest,
    ActivityRunResponse,
    SeedActivityListResponse,
    SeedActivityOut,
)

logger = logging.getLogger("youhue.activities")
router = APIRouter(prefix="/activities", tags=["activities"])


@router.get("/seed", response_model=SeedActivityListResponse)
def list_seed_activities(staff: StaffDep, db: DbDep) -> SeedActivityListResponse:  # noqa: ARG001
    """Minimal-GET-add — see `schemas.activities.SeedActivityListResponse` docstring. Read-only, no
    transaction to commit/roll back. `staff` is unused beyond the `StaffDep` auth gate itself (any
    authenticated staff member may read the global seed set)."""
    activities = activities_svc.list_seed_activities_for_staff(db)
    return SeedActivityListResponse(
        activities=[
            SeedActivityOut(id=a.id, title=a.title, type=a.type.value, topic=a.topic)
            for a in activities
        ]
    )


@router.post(
    "/{activity_id}/run",
    response_model=ActivityRunResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Target class/student is not in the caller's own/shared class scope.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such activity, class, or student."},
        status.HTTP_422_UNPROCESSABLE_ENTITY: {"description": "Malformed target."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Could not run/assign the activity.",
        },
    },
)
def run_or_assign_activity(
    activity_id: uuid.UUID, body: ActivityRunRequest, staff: StaffDep, db: DbDep
) -> ActivityRunResponse:
    """200 `{ assigned }` — the student ids the activity was assigned to. `target` is `class:{id}`
    (run with the whole class) or `student:{id}` (assign to one student in the caller's own/shared
    class); 403 outside that scope, 404 unknown activity/target, 422 a malformed target."""
    try:
        assigned = activities_svc.run_or_assign_activity(db, staff, activity_id, body.target)
    except HTTPException:
        db.commit()  # persist the audit-visible reject/forbid path before surfacing the error
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception(
            "fr_14_02_error action=run_or_assign actor_id=%s activity_id=%s", staff.id, activity_id
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not run/assign the activity"
        ) from exc
    db.commit()
    return ActivityRunResponse(assigned=assigned)
