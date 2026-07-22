"""Risk-scoring endpoint (INFRA-06). Internal: invoked on check-in submit to score + flag.
Thin router — scoring logic lives in src.application.risk.services. School-scoped: a session may
only score its own school's check-ins."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.application.risk import services as risk_svc
from src.domain.checkin import services as checkin_db
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep, require_same_school
from src.schemas.risk import ScoreRequest, ScoreResponse

logger = logging.getLogger("youhue.risk")
router = APIRouter(prefix="/risk", tags=["risk"])


@router.post("/score", response_model=ScoreResponse)
def score(body: ScoreRequest, sess: SessionDep, db: DbDep) -> ScoreResponse:
    checkin = checkin_db.get_checkin(db, body.checkin_id)
    if checkin is None:
        logger.info("fr_12_01_rejected checkin=%s reason=not_found", body.checkin_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Check-in not found")
    try:  # the single cross-tenant guard — a session never scores another school's check-in
        require_same_school(sess, checkin.school_id)
    except HTTPException:
        logger.warning("fr_12_01_forbidden checkin=%s", body.checkin_id)
        raise
    result = risk_svc.score_checkin(db, checkin)
    db.commit()
    return ScoreResponse(
        flagged=result.flagged, risk_score=result.risk_score, matched_terms=result.matched_terms
    )
