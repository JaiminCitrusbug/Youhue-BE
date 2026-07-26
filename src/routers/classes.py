"""Class listing (FR-02-03 delta addition): InviteColleague.tsx (SC-059) needs the caller's OWNED
classes to populate its 'Shared class' picker with real data, never a fixture — no prior ticket
exposed a class-list read endpoint. Mirrors the established minimal-GET-add precedent (FR-02-02's
two read endpoints, FR-03-05's ``GET /schools/{id}/roster``): not in the ticket's literal "What to
build", added because rendering the approved screen honestly requires something real to read.

FR-10-01 adds the class dashboard read.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status

from src.application.classes import services as classes_svc
from src.application.dashboard import services as dashboard_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.classes import MyClassesResponse
from src.schemas.dashboard import ClassDashboardOut, ClassRosterOut

logger = logging.getLogger("youhue.dashboard")
router = APIRouter(prefix="/classes", tags=["classes"])


@router.get("/mine", response_model=MyClassesResponse)
def get_my_classes(staff: StaffDep, db: DbDep) -> MyClassesResponse:
    """Read-only — no transaction to commit/roll back."""
    return classes_svc.list_owned_classes(db, staff)


@router.get(
    "/{class_id}/dashboard",
    response_model=ClassDashboardOut,
    responses={
        403: {"description": "Not this staff member's own/shared class."},
        422: {"description": "Unknown range value."},
    },
)
def get_class_dashboard(
    class_id: uuid.UUID,
    staff: StaffDep,
    db: DbDep,
    range: str | None = Query(default=None, description="this_week|month|term|around:{date}"),
) -> ClassDashboardOut:
    """FR-10-01/FR-10-03 — read-only, no transaction to commit/roll back. `range` omitted defaults
    to `this_week` (unchanged FR-10-01 behaviour)."""
    try:
        return dashboard_svc.get_class_dashboard(db, staff, class_id, range)
    except HTTPException:
        raise  # already logged (fr_10_03_forbidden / _rejected) at the point it was raised
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak an unhandled 500
        logger.exception(
            "fr_10_03_error action=get_dashboard actor_id=%s class_id=%s", staff.id, class_id
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not load the dashboard"
        ) from exc


@router.get(
    "/{class_id}/roster",
    response_model=ClassRosterOut,
    responses={403: {"description": "Not this staff member's own/shared class."}},
)
def get_class_roster(class_id: uuid.UUID, staff: StaffDep, db: DbDep) -> ClassRosterOut:
    """FR-10-02 — the class's real student rows, for the dashboard's "click into a single
    student" (Scenario 1). Read-only, no transaction to commit/roll back."""
    return dashboard_svc.get_class_roster(db, staff, class_id)
