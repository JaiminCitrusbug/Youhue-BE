"""Flag timeline read (FR-12-09). The immutable, append-only audit trail around a Flag — who was
alerted, who viewed, who acted, who was escalated-to, and when. The `FlagEvent` table + its DB-level
append-only trigger already exist (INFRA-02 migration 8ab7f8057e0a; immutability trigger
c0ffee000001), and FR-12-04 already writes the `alerted` row (`risk_db.record_flag_alerted`, called
from `dispatch_alert`) — this ticket adds ONLY the read surface below. `viewed`/`acted` are written
by FR-13-04/05 and `escalated` by FR-12-08, neither of which exists yet; this endpoint renders
whatever rows are present. Thin router — the read itself lives in `src.domain.risk.services`."""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.constants.enums import SessionKind
from src.domain.identity import services as identity_db
from src.domain.risk import services as risk_db
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep, require_same_school
from src.schemas.risk import FlagEventOut, FlagEventsResponse

logger = logging.getLogger("youhue.risk")
router = APIRouter(prefix="/flags", tags=["flags"])


@router.get("/{flag_id}/events", response_model=FlagEventsResponse)
def list_events(flag_id: uuid.UUID, sess: SessionDep, db: DbDep) -> FlagEventsResponse:
    """Read restricted to the flag's own school's staff (403 otherwise), school-scoped — same
    posture as the sibling /risk/* and /alerts/* endpoints (no new authz pattern). Append-only /
    immutable: no PATCH or DELETE route exists on this resource anywhere in this router."""
    if sess.kind != SessionKind.staff:
        logger.warning("fr_12_09_forbidden reason=non_staff kind=%s", sess.kind.value)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff session required")
    flag = risk_db.get_flag(db, flag_id)
    if flag is None:
        logger.info("fr_12_09_rejected flag=%s reason=not_found", flag_id)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flag not found")
    try:  # the single cross-tenant guard — a staff session never reads another school's timeline
        require_same_school(sess, flag.school_id)
    except HTTPException:
        logger.warning("fr_12_09_forbidden flag=%s reason=cross_tenant", flag_id)
        raise
    try:
        events = risk_db.list_flag_events(db, flag_id)
        out = []
        for e in events:
            actor_email = None
            if e.actor_id is not None:
                actor = identity_db.get_staff(db, e.actor_id)
                actor_email = actor.email if actor else None
            out.append(FlagEventOut(type=e.event_type.value, actor=actor_email, at=e.at))
    except Exception as exc:  # noqa: BLE001 - a read never fails silently (Baseline BR-05)
        logger.exception("fr_12_09_error flag=%s", flag_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not load flag timeline"
        ) from exc
    logger.info("fr_12_09_success flag=%s count=%s", flag_id, len(out))
    return FlagEventsResponse(events=out)
