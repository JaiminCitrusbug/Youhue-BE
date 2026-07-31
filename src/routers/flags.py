"""Flag-response endpoints.

GET /flags/{id}/events (FR-12-09) — the immutable, append-only audit trail around a Flag: who was
alerted, who viewed, who acted, who was escalated-to, and when. The `FlagEvent` table + its DB-level
append-only trigger already exist (INFRA-02 migration 8ab7f8057e0a; immutability trigger
c0ffee000001), and FR-12-04 already writes the `alerted` row (`risk_db.record_flag_alerted`, called
from `dispatch_alert`) — FR-12-09 adds ONLY the read surface below. `viewed`/`acted` are written by
FR-13-04/05 and `escalated` by FR-12-08; this endpoint renders whatever rows are present.

GET /flags/{id}/guidance (FR-13-04) — advisory wording/next-steps/links for the teacher opening the
response to a flagged check-in. No persistence, read-only.

GET /flags/{id}/student (FR-13-05) — resolves a flag to its student (id + display name) so the
guided-response screen's "send a private note" action can navigate somewhere real; deliberately
NOT merged into GuidanceOut (frozen by FR-13-04's own exact-keys test).

Thin router — business logic lives in src.domain.risk.services (events) and
src.application.risk.services (guidance, flag->student resolution).
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.risk import services as risk_svc
from src.constants.enums import SessionKind
from src.domain.identity import services as identity_db
from src.domain.risk import services as risk_db
from src.infrastructure.middlewares.auth_middleware import (
    DbDep,
    SessionDep,
    StaffDep,
    require_same_school,
)
from src.schemas.risk import (
    FlagEventOut,
    FlagEventsResponse,
    FlagStudentOut,
    GuidanceLinkOut,
    GuidanceOut,
)

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


@router.get(
    "/{flag_id}/guidance",
    response_model=GuidanceOut,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not the involved teacher for this flag, or cross-tenant.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such flag."},
    },
)
def get_guidance(flag_id: uuid.UUID, staff: StaffDep, db: DbDep) -> GuidanceOut:
    """FR-13-04 (SC-040): suggested wording, sensible next steps and useful links for a teacher
    responding to a worrying check-in. Advisory only — the teacher may use, adapt or ignore every
    suggestion; nothing here forces, requires or auto-applies an action (ticket §Must-nots)."""
    try:
        guidance = risk_svc.get_guidance(db, staff, flag_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard, never leak an unhandled 500
        logger.exception("fr_13_04_error action=get_guidance flag_id=%s", flag_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve guidance"
        ) from exc
    return GuidanceOut(
        suggested_wording=guidance.suggested_wording,
        next_steps=guidance.next_steps,
        links=[GuidanceLinkOut(label=label) for label in guidance.links],
    )


@router.get(
    "/{flag_id}/student",
    response_model=FlagStudentOut,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Caller is not the involved teacher for this flag, or cross-tenant.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such flag."},
    },
)
def get_flag_student(flag_id: uuid.UUID, staff: StaffDep, db: DbDep) -> FlagStudentOut:
    """FR-13-05: the minimal identity a flag resolves to (id + display name), so the guided
    -response "send a private note" action has a real `student_id` to POST to and a real name to
    show. Same involved-teacher gate as `/guidance` above (reused, not reinvented)."""
    try:
        student = risk_svc.get_flag_student(db, staff, flag_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard, never leak an unhandled 500
        logger.exception("fr_13_05_error action=resolve_flag_student flag_id=%s", flag_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve this flag's student"
        ) from exc
    return FlagStudentOut(student_id=student.id, student_name=student.display_name)
