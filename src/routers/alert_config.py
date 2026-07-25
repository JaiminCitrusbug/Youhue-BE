"""Alert recipient configuration (FR-12-05). Thin router — business logic lives in
src.application.risk.services. Leadership-only, school-scoped (403 otherwise, INFRA-03 §8, no
exceptions). Distinct ticket/ownership from FR-16-02's settings PATCH (src/routers/leadership.py),
which shares the same underlying table but not this endpoint's contract or log namespace — this
file owns ONLY the dedicated PUT /alert-config surface the ticket's DoD specifies.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.risk import services as risk_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.risk import AlertConfigOut, AlertConfigRequest, AlertConfigResponse

logger = logging.getLogger("youhue.risk")
router = APIRouter(prefix="/schools", tags=["alert-config"])


@router.put(
    "/{school_id}/alert-config",
    response_model=AlertConfigResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not leadership, or a different school."
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "description": "Unsupported alert type, or a recipient not on this school's staff."
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Update failed; nothing was written."
        },
    },
)
def set_alert_config(
    school_id: uuid.UUID, body: AlertConfigRequest, staff: StaffDep, db: DbDep
) -> AlertConfigResponse:
    """SC-061 — set the ordered recipient chain for one alert_type; arms the alert route
    (GATE G-5)."""
    try:
        row = risk_svc.set_alert_config(
            db, staff, school_id, body.alert_type, body.recipient_staff_ids
        )
    except HTTPException:
        db.rollback()  # no partial write survives a forbidden/invalid-value call
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_12_05_error action=set_alert_config")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Update failed") from exc
    db.commit()
    return AlertConfigResponse(
        config=AlertConfigOut(
            alert_type=row.alert_type, recipient_staff_ids=list(row.recipient_staff_ids)
        )
    )
