"""School leadership hub (FR-16-02): staff management (SC-057) + the leadership-exposed settings
surfaces (SC-060 concern words · SC-061 alert routing · SC-063 access window), all scoped to the
leader's OWN school. Thin router — business logic lives in ``src.application.leadership.services``.
Leadership-only, same-school-only (403 otherwise, INFRA-03 §8, no exceptions). Every write is one
ACID transaction — commit on success, roll back on any error; an unexpected failure surfaces a
generic 500 and an ``fr_16_02_error`` log.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.leadership import services as leadership_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.settings import (
    ErrorResponse as SettingsErrorResponse,
)
from src.schemas.settings import (
    SchoolSettingsResponse,
    SchoolSettingsUpdate,
)
from src.schemas.staff import (
    ErrorResponse as StaffErrorResponse,
)
from src.schemas.staff import (
    StaffListResponse,
    StaffOut,
    StaffUpdateRequest,
    StaffUpdateResponse,
)

logger = logging.getLogger("youhue.leadership")
router = APIRouter(prefix="/schools", tags=["leadership"])


@router.get(
    "/{school_id}/staff",
    response_model=StaffListResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": StaffErrorResponse,
            "description": "Caller is not leadership, or belongs to a different school.",
        },
    },
)
def get_staff_list(school_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StaffListResponse:
    """SC-057 — the school's staff list (any status)."""
    rows = leadership_svc.list_staff(db, staff, school_id)
    return StaffListResponse(staff=[StaffOut.model_validate(r) for r in rows])


@router.patch(
    "/{school_id}/staff/{staff_id}",
    response_model=StaffUpdateResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": StaffErrorResponse,
            "description": "Caller is not leadership, or belongs to a different school.",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": StaffErrorResponse,
            "description": "No such staff member at this school.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": StaffErrorResponse,
            "description": "Unsupported status value.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": StaffErrorResponse,
            "description": "Update failed; nothing was written.",
        },
    },
)
def update_staff(
    school_id: uuid.UUID,
    staff_id: uuid.UUID,
    body: StaffUpdateRequest,
    staff: StaffDep,
    db: DbDep,
) -> StaffUpdateResponse:
    """SC-057 — deactivate a colleague. Deactivation withdraws access immediately (see
    ``application.leadership.services.deactivate_staff`` docstring for how)."""
    try:
        result = leadership_svc.deactivate_staff(db, staff, school_id, staff_id, body.status)
    except HTTPException:
        db.rollback()  # no partial write survives a forbidden/not-found/invalid-status call
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_16_02_error action=update_staff")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Update failed") from exc
    db.commit()
    return StaffUpdateResponse(staff=StaffOut.model_validate(result))


@router.get(
    "/{school_id}/settings",
    response_model=SchoolSettingsResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": SettingsErrorResponse,
            "description": "Caller is not leadership, or belongs to a different school.",
        },
    },
)
def get_settings(school_id: uuid.UUID, staff: StaffDep, db: DbDep) -> SchoolSettingsResponse:
    """SC-060/061/063 — the current snapshot of every leadership-exposed setting."""
    return SchoolSettingsResponse(settings=leadership_svc.get_settings(db, staff, school_id))


@router.patch(
    "/{school_id}/settings",
    response_model=SchoolSettingsResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": SettingsErrorResponse,
            "description": "Caller is not leadership, or belongs to a different school.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": SettingsErrorResponse,
            "description": "Invalid setting value, or zero/more-than-one setting in one call.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": SettingsErrorResponse,
            "description": "Update failed; nothing was written.",
        },
    },
)
def update_settings(
    school_id: uuid.UUID, body: SchoolSettingsUpdate, staff: StaffDep, db: DbDep
) -> SchoolSettingsResponse:
    """SC-060/061/063 — apply ONE changed leadership-exposed setting; returns the full snapshot."""
    try:
        settings_out = leadership_svc.update_settings(db, staff, school_id, body)
    except HTTPException:
        db.rollback()  # no partial write survives a forbidden/invalid-value call
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_16_02_error action=update_settings")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Update failed") from exc
    db.commit()
    return SchoolSettingsResponse(settings=settings_out)
