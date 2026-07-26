"""FR-02-04 — the general staff-account lifecycle status endpoint (SC-057, lifecycle accuracy
only; the leadership deactivation UI/endpoint itself stays owned by FR-16-02, unchanged). Thin
router — the state machine lives in ``src.application.staff_lifecycle.services``.
"""
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.staff_lifecycle import services as staff_lifecycle_svc
from src.constants.enums import StaffStatus
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.staff import StaffOut
from src.schemas.staff_lifecycle import (
    ErrorResponse,
    StaffStatusUpdateRequest,
    StaffStatusUpdateResponse,
)

router = APIRouter(prefix="/staff", tags=["staff-lifecycle"])


@router.patch(
    "/{staff_id}/status",
    response_model=StaffStatusUpdateResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Caller is not leadership, or the target belongs to another school.",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse, "description": "No such staff account.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse, "description": "Illegal status transition.",
        },
    },
)
def set_staff_status(
    staff_id: uuid.UUID, body: StaffStatusUpdateRequest, staff: StaffDep, db: DbDep
) -> StaffStatusUpdateResponse:
    try:
        result = staff_lifecycle_svc.set_staff_status(
            db, staff, staff_id, StaffStatus(body.status)
        )
    except HTTPException:
        db.rollback()
        raise
    db.commit()
    return StaffStatusUpdateResponse(
        status=result.status.value, staff=StaffOut.model_validate(result)
    )
