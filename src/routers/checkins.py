"""Daily check-in submit (FR-04-01, SC-023). Student-only, self-only: `StudentDep` resolves the
CALLER's own account off the session — there is no `student_id` in the request body, so a student
can never submit on behalf of anyone else. Thin router — business logic lives in
``src.application.checkin.services``."""
import logging

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy.orm import Session

from src.application.checkin import services as checkin_svc
from src.application.risk import services as risk_svc
from src.domain.checkin.models import CheckIn
from src.infrastructure.middlewares.auth_middleware import DbDep, StudentDep
from src.schemas.checkin import (
    CheckInConfigOut,
    CheckInCreate,
    CheckInOut,
    CheckInSyncCreate,
    ErrorResponse,
)

logger = logging.getLogger("youhue.checkin")
router = APIRouter(prefix="/check-ins", tags=["check-ins"])


@router.get(
    "/config",
    response_model=CheckInConfigOut,
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Could not resolve the caller's check-in config.",
        },
    },
)
def get_checkin_config(student: StudentDep) -> CheckInConfigOut:
    """FR-04-03 — the CALLER's own age-matched check-in config (mode/mood_set/read_aloud);
    supersedes FR-04-01's `GET /mood-set`. The mood-selection screen reads this before rendering
    its faces."""
    try:
        mode, mood_set, read_aloud = checkin_svc.get_checkin_config(student)
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception(
            "fr_04_03_error action=get_checkin_config student_id=%s", student.id
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve check-in config"
        ) from exc
    logger.info(
        "fr_04_03_success action=get_checkin_config student_id=%s mode=%s", student.id, mode
    )
    return CheckInConfigOut(mode=mode, mood_set=mood_set, read_aloud=read_aloud)


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
        _score_after_create(db, checkin)
    return CheckInOut(checkin_id=checkin.id, activity_offer=None)


def _score_after_create(db: Session, checkin: CheckIn) -> None:
    """FR-12-01 (closes DEF-005 — "who calls scoring on submit"): score the check-in for
    adult-attention flagging right after the write durably lands. A scoring failure never fails the
    check-in response — the row stays `scored=False` and the existing `process_pending` worker
    (INFRA-06) retries/dead-letters it (surfaced CRITICAL, never silently dropped). Shared by both
    `submit_checkin` and `sync_checkin` (FR-04-06) — an offline-originated check-in is scored the
    same way as an online one, on its first successful write."""
    try:
        risk_svc.score_checkin(db, checkin)
        db.commit()
    except Exception:  # noqa: BLE001 — scoring must never fail the check-in response
        db.rollback()
        logger.exception("fr_12_01_error action=submit_checkin checkin_id=%s", checkin.id)


@router.post(
    "/sync",
    response_model=CheckInOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_200_OK: {
            "model": CheckInOut,
            "description": "Idempotent replay: this client_entry_id was already synced; the "
            "existing entry is returned, nothing new is created.",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Outside the school's access window/holiday, or parental consent is "
            "not verified (evaluated at SYNC time, not capture time).",
        },
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "A DIFFERENT check-in already exists for today (same one-per-day rule "
            "as the online submit path).",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "Invalid mood value for the caller's age band, or a reflection is "
            "required by the school and missing.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Could not record the synced check-in.",
        },
    },
)
def sync_checkin(
    body: CheckInSyncCreate, student: StudentDep, db: DbDep, response: Response
) -> CheckInOut:
    """FR-04-06 — submit a check-in that was captured while the device was offline.
    `client_entry_id` is the idempotency key: 201 on first create, 200 on a retried sync of the same
    entry (never double-creates — ticket §Interaction contract [BR-05])."""
    try:
        checkin, created = checkin_svc.sync_checkin(
            db, student, body.client_entry_id, body.mood_value, body.reflection_text
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception("fr_04_06_error action=sync_checkin student_id=%s", student.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not record the synced check-in"
        ) from exc
    db.commit()
    if created:
        _score_after_create(db, checkin)
        response.status_code = status.HTTP_201_CREATED
    else:
        response.status_code = status.HTTP_200_OK
    return CheckInOut(checkin_id=checkin.id, activity_offer=None)
