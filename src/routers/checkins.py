"""Daily check-in submit (FR-04-01, SC-023). Student-only, self-only: `StudentDep` resolves the
CALLER's own account off the session — there is no `student_id` in the request body, so a student
can never submit on behalf of anyone else. Thin router — business logic lives in
``src.application.checkin.services``."""
import logging

from fastapi import APIRouter, HTTPException, status

from src.application.checkin import services as checkin_svc
from src.application.risk import services as risk_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StudentDep
from src.schemas.checkin import CheckInCreate, CheckInOut, ErrorResponse, MoodSetOut

logger = logging.getLogger("youhue.checkin")
router = APIRouter(prefix="/check-ins", tags=["check-ins"])


@router.get("/mood-set", response_model=MoodSetOut)
def get_mood_set(student: StudentDep) -> MoodSetOut:
    """The age-appropriate mood set (config-driven, ticket Q-3) for the CALLER's own age band —
    the mood-selection screen reads this before rendering its faces."""
    return MoodSetOut(values=checkin_svc.get_mood_set(student))


@router.post(
    "",
    response_model=CheckInOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Outside the school's access window/holiday, or parental consent is "
            "not verified.",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "Already checked in today with different content.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "Invalid mood value for the caller's age band, or a reflection is "
            "required by the school and missing.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Could not record the check-in.",
        },
    },
)
def submit_checkin(body: CheckInCreate, student: StudentDep, db: DbDep) -> CheckInOut:
    """201 `{checkin_id, activity_offer}` on success — `activity_offer` is always `null` here (see
    ``src.schemas.checkin.ActivityOfferOut`` docstring: it is a typed stub for FR-05-01, a later
    ticket, not a functional stub of this endpoint)."""
    try:
        checkin, created = checkin_svc.submit_checkin(
            db, student, body.mood_value, body.reflection_text
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception("fr_04_01_error action=submit_checkin student_id=%s", student.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not record check-in"
        ) from exc
    db.commit()
    if created:
        # FR-12-01 (closes DEF-005 — "who calls scoring on submit"): score the check-in for
        # adult-attention flagging right after the write durably lands. A scoring failure never
        # fails the check-in response — the row stays `scored=False` and the existing
        # process_pending worker (INFRA-06) retries/dead-letters it (surfaced CRITICAL, never
        # silently dropped) — see docs/tickets/FR-12-01.md.
        try:
            risk_svc.score_checkin(db, checkin)
            db.commit()
        except Exception:  # noqa: BLE001 — scoring must never fail the check-in response
            db.rollback()
            logger.exception(
                "fr_12_01_error action=submit_checkin checkin_id=%s", checkin.id
            )
    return CheckInOut(checkin_id=checkin.id, activity_offer=None)
