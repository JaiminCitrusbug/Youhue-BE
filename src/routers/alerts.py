"""Alert dispatch (FR-12-04, GATE G-7) + escalation (FR-12-08, GATE G-8) endpoints. Internal/system
surface — dispatch is invoked once a check-in has been routed to the immediate band (FR-12-06) and
hands off to the school's configured adults (FR-12-05); escalation walks that same ordered
recipient list when a dispatched alert goes unacknowledged, both over the existing notification
transport (INFRA-05). Thin router — business logic lives in src.application.risk.services."""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.risk import services as risk_svc
from src.constants.enums import FlagBand, SessionKind
from src.domain.risk import services as risk_db
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep, require_same_school
from src.schemas.risk import (
    AlertAcknowledgeResponse,
    AlertDispatchRequest,
    AlertDispatchResponse,
    AlertEscalateResponse,
)

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


@router.post("/{flag_id}/escalate", response_model=AlertEscalateResponse)
def escalate(flag_id: uuid.UUID, sess: SessionDep, db: DbDep) -> AlertEscalateResponse:
    """FR-12-08 (GATE G-8): escalate an alerted, unacknowledged flag to the next configured adult
    in FR-12-05's ordered recipient list. Same posture as /dispatch — staff-session-gated,
    school-scoped (BR-01). The env-configurable ack-timeout GATE is enforced by the scheduled
    caller (`process_due_escalations`), not this endpoint itself — an on-demand/manual escalate
    call is just as legitimate a caller of this endpoint as the scheduler, same relationship
    /dispatch has to FR-12-06's inline call. GATE G-8's own guard (an ACKNOWLEDGED flag returns
    409) and idempotency on retry (BR-05) both live in `risk_svc.escalate_alert`."""
    if sess.kind != SessionKind.staff:
        logger.warning(
            "fr_12_08_forbidden action=escalate reason=non_staff kind=%s", sess.kind.value
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    flag = risk_db.get_flag(db, flag_id)
    if flag is None:
        logger.info("fr_12_08_rejected action=escalate flag=%s reason=not_found", flag_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flag not found")
    try:  # the single cross-tenant guard — a staff session never escalates another school's flag
        require_same_school(sess, flag.school_id)
    except HTTPException:
        logger.warning("fr_12_08_forbidden action=escalate flag=%s reason=cross_tenant", flag_id)
        raise
    try:
        escalated_to = risk_svc.escalate_alert(db, flag)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_12_08_error action=escalate flag=%s", flag_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not escalate alert"
        ) from exc
    db.commit()
    return AlertEscalateResponse(flag_id=flag.id, escalated_to=escalated_to)


@router.post("/{flag_id}/acknowledge", response_model=AlertAcknowledgeResponse)
def acknowledge(flag_id: uuid.UUID, sess: SessionDep, db: DbDep) -> AlertAcknowledgeResponse:
    """Structural minimum FR-12-08 needs to make GATE G-8 (an acknowledged alert does not
    escalate) testable end-to-end — no acknowledge concept/endpoint pre-existed anywhere in this
    codebase before this ticket. Same posture as /dispatch and /escalate."""
    if sess.kind != SessionKind.staff:
        logger.warning(
            "fr_12_08_forbidden action=acknowledge reason=non_staff kind=%s", sess.kind.value
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    flag = risk_db.get_flag(db, flag_id)
    if flag is None:
        logger.info("fr_12_08_rejected action=acknowledge flag=%s reason=not_found", flag_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flag not found")
    try:  # the single cross-tenant guard — a staff session never acknowledges another school's flag
        require_same_school(sess, flag.school_id)
    except HTTPException:
        logger.warning("fr_12_08_forbidden action=acknowledge flag=%s reason=cross_tenant", flag_id)
        raise
    try:
        risk_svc.acknowledge_alert(db, flag)
    except Exception as exc:  # noqa: BLE001 - last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_12_08_error action=acknowledge flag=%s", flag_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not acknowledge alert"
        ) from exc
    db.commit()
    return AlertAcknowledgeResponse(flag_id=flag.id, status=flag.status.value)
