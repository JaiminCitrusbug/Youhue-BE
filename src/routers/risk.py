"""Risk-scoring endpoint (INFRA-06). Internal: invoked on check-in submit to score + flag.
Thin router — scoring logic lives in src.application.risk.services. School-scoped: a session may
only score its own school's check-ins."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.application.risk import services as risk_svc
from src.constants.enums import SessionKind
from src.domain.checkin import services as checkin_db
from src.domain.identity import services as identity_db
from src.domain.risk import services as risk_db
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep, require_same_school
from src.schemas.risk import (
    RouteRequest,
    RouteResponse,
    ScoreRequest,
    ScoreResponse,
    SlowBurnEvaluateRequest,
    SlowBurnEvaluateResponse,
    TriageQueueItem,
    TriageQueueResponse,
)

logger = logging.getLogger("youhue.risk")
router = APIRouter(prefix="/risk", tags=["risk"])


@router.post("/score", response_model=ScoreResponse)
def score(body: ScoreRequest, sess: SessionDep, db: DbDep) -> ScoreResponse:
    # Internal/staff surface — a student session never scores (would expose a peer's matched terms).
    if sess.kind != SessionKind.staff:
        logger.warning("fr_12_01_forbidden reason=non_staff kind=%s", sess.kind.value)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    checkin = checkin_db.get_checkin(db, body.checkin_id)
    if checkin is None:
        logger.info("fr_12_01_rejected checkin=%s reason=not_found", body.checkin_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check-in not found")
    try:  # the single cross-tenant guard — a staff session never scores another school's check-in
        require_same_school(sess, checkin.school_id)
    except HTTPException:
        logger.warning("fr_12_01_forbidden checkin=%s", body.checkin_id)
        raise
    result = risk_svc.score_checkin(db, checkin)
    db.commit()
    return ScoreResponse(
        flagged=result.flagged, risk_score=result.risk_score, matched_terms=result.matched_terms
    )


@router.post("/route", response_model=RouteResponse)
def route(body: RouteRequest, sess: SessionDep, db: DbDep) -> RouteResponse:
    """FR-12-06 (GATE G-9): route a scored check-in to exactly one band — immediate alert, triage
    queue, or no action. Staff-only, school-scoped, same posture as /score. On completion this
    surfaces the flag (persists the band, and for immediate hands off to already-configured
    adults) and takes NO other action on the student."""
    if sess.kind != SessionKind.staff:
        logger.warning("fr_12_06_forbidden reason=non_staff kind=%s", sess.kind.value)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    checkin = checkin_db.get_checkin(db, body.checkin_id)
    if checkin is None:
        logger.info("fr_12_06_rejected checkin=%s reason=not_found", body.checkin_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check-in not found")
    try:
        require_same_school(sess, checkin.school_id)
    except HTTPException:
        logger.warning("fr_12_06_forbidden checkin=%s", body.checkin_id)
        raise
    try:
        band, flag_id = risk_svc.route_checkin(db, body.checkin_id)
    except Exception as exc:
        db.rollback()
        logger.exception("fr_12_06_error checkin=%s", body.checkin_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not route check-in"
        ) from exc
    db.commit()
    return RouteResponse(band=band.value, flag_id=flag_id)


@router.get("/triage", response_model=TriageQueueResponse)
def triage_queue(sess: SessionDep, db: DbDep) -> TriageQueueResponse:
    """SC-038 — the caller's own school's open, triage-band flags for human review. Render-only
    (GATE G-9): a read never performs an action on a student."""
    if sess.kind != SessionKind.staff:
        logger.warning("fr_12_06_forbidden reason=non_staff kind=%s", sess.kind.value)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    staff = identity_db.get_staff(db, sess.subject_id)
    if staff is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    flags = risk_db.list_open_triage_flags(db, staff.school_id)
    logger.info("fr_12_06_success action=triage_queue staff=%s count=%s", staff.id, len(flags))
    items = []
    for f in flags:
        student = identity_db.get_student(db, f.student_id)
        items.append(
            TriageQueueItem(
                flag_id=f.id,
                student_id=f.student_id,
                student_name=student.display_name if student else "Unknown",
                type=f.type.value,
                risk_score=float(f.risk_score),
                created_at=f.created_at,
            )
        )
    return TriageQueueResponse(flags=items)


@router.post("/slow-burn/evaluate", response_model=SlowBurnEvaluateResponse)
def evaluate_slow_burn(
    body: SlowBurnEvaluateRequest, sess: SessionDep, db: DbDep
) -> SlowBurnEvaluateResponse:
    """FR-12-03: evaluate a student's slow-burn trend over their existing check-in history — a
    scheduled/background system process, not tied to a new submission (unlike /score, which fires
    inline on submit). Staff-only, school-scoped, same posture as /score."""
    if sess.kind != SessionKind.staff:
        logger.warning("fr_12_03_forbidden reason=non_staff kind=%s", sess.kind.value)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    student = identity_db.get_student(db, body.student_id)
    if student is None:
        logger.info("fr_12_03_rejected student=%s reason=not_found", body.student_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    try:
        require_same_school(sess, student.school_id)
    except HTTPException:
        logger.warning("fr_12_03_forbidden student=%s", body.student_id)
        raise
    try:
        flag_raised = risk_svc.evaluate_slow_burn(db, student.id, student.school_id)
    except Exception as exc:
        db.rollback()
        logger.exception("fr_12_03_error student=%s", body.student_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not evaluate slow-burn trend"
        ) from exc
    db.commit()
    logger.info("fr_12_03_success student=%s flag_raised=%s", body.student_id, flag_raised)
    return SlowBurnEvaluateResponse(flag_raised=flag_raised)
