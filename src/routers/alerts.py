"""Alert dispatch endpoint (FR-12-04, GATE G-7). Internal/system surface — invoked once a check-in
has been routed to the immediate band (FR-12-06); hands off to the school's configured adults
(FR-12-05) over the existing notification transport (INFRA-05). Thin router — business logic lives
in src.application.risk.services."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.application.risk import services as risk_svc
from src.constants.enums import FlagBand, SessionKind
from src.domain.risk import services as risk_db
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep, require_same_school
from src.schemas.risk import AlertDispatchRequest, AlertDispatchResponse

logger = logging.getLogger("youhue.risk")
router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.post(
    "/dispatch", response_model=AlertDispatchResponse, status_code=status.HTTP_202_ACCEPTED
)
def dispatch(body: AlertDispatchRequest, sess: SessionDep, db: DbDep) -> AlertDispatchResponse:
    """FR-12-04 (GATE G-7): dispatch an immediate-band flag's alert to its school's configured
    adults by email + in-app, reusing FR-12-05's recipient config and INFRA-05's transport. Same
    posture as /risk/route — a scheduled/background system process, staff-session-gated,
    school-scoped (BR-01). Idempotent on retry (BR-05): a flag already alerted is not re-alerted."""
    if sess.kind != SessionKind.staff:
        logger.warning("fr_12_04_forbidden reason=non_staff kind=%s", sess.kind.value)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    flag = risk_db.get_flag(db, body.flag_id)
    if flag is None:
        logger.info("fr_12_04_rejected flag=%s reason=not_found", body.flag_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flag not found")
    try:  # the single cross-tenant guard — a staff session never dispatches another school's flag
        require_same_school(sess, flag.school_id)
    except HTTPException:
        logger.warning("fr_12_04_forbidden flag=%s reason=cross_tenant", body.flag_id)
        raise
    if flag.band != FlagBand.immediate:
        logger.info("fr_12_04_rejected flag=%s reason=not_immediate_band", body.flag_id)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Flag is not in the immediate-alert band"
        )
    try:
        recipients = risk_svc.dispatch_alert(db, flag)
    except Exception as exc:  # noqa: BLE001 - last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_12_04_error flag=%s", body.flag_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not dispatch alert"
        ) from exc
    db.commit()
    return AlertDispatchResponse(flag_id=flag.id, recipients=recipients)
