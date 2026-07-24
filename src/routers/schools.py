"""School surface: self-registration (FR-02-01, public) + District/Trust approval (FR-02-02, staff).

Registration is public (unauthenticated) — a teacher self-registers before they have any account.
Approval/rejection and its two read endpoints require an authenticated, District-role staff session
(``StaffDep``) — see ``decide_school`` and its neighbours below. Thin router: all business logic
lives in ``src.application.schools.services``. Registration carries its OWN rate-limit bucket
(``rate_limit_registration``) so this anonymous, internet-facing write cannot spend the auth
budget legitimate staff sign-ins depend on. Every write here is one ACID transaction — commit on
success, roll back on any error; an unexpected failure surfaces a generic 500 and an ``_error`` log.

Every status this endpoint can return is declared in ``responses`` so the generated OpenAPI client
knows them. Note the shape asymmetry the frontend must type-guard: ``detail`` is an OBJECT for 409
and a plain STRING for 403/404/429/500, but FastAPI's built-in 422 is an ARRAY of
``{loc,msg,type}``.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from src.application.schools import services as schools_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.infrastructure.middlewares.ratelimit import rate_limit_registration
from src.schemas.schools import (
    ConflictResponse,
    DecisionConflictResponse,
    ErrorResponse,
    PendingSchoolsResponse,
    RegisterSchool,
    RegisterSchoolResponse,
    SchoolDecisionRequest,
    SchoolDecisionResponse,
    SchoolDetailResponse,
)

logger = logging.getLogger("youhue.schools")
router = APIRouter(prefix="/schools", tags=["schools"])
_throttle = [Depends(rate_limit_registration)]


@router.post(
    "",
    response_model=RegisterSchoolResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_throttle,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": ConflictResponse,
            "description": (
                "A school with this name is already registered. Terminal for this endpoint: ask a "
                "colleague to invite you. Also returned when an existing pending registration for "
                "this name is not proven by the submitted password."
            ),
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": ErrorResponse,
            "description": "Registration rate limit for this client exceeded.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Registration failed; nothing was written.",
        },
    },
)
def register_school(body: RegisterSchool, db: DbDep) -> RegisterSchoolResponse:
    try:
        result = schools_svc.register_school(
            db, body.school_name, body.registrant_email, body.password
        )
    except HTTPException:
        db.rollback()  # no partial write survives a rejected/forbidden registration
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_02_01_error")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Registration failed"
        ) from exc
    db.commit()
    return result


# --- FR-02-02: District/Trust leadership reviews + decides a pending school --------------------
# Registered BEFORE "/{school_id}" — a literal path must win over the UUID-typed param route, or
# GET /schools/pending would 422 trying to parse "pending" as a UUID instead of matching this route.


@router.get("/pending", response_model=PendingSchoolsResponse)
def get_pending_schools(staff: StaffDep, db: DbDep) -> PendingSchoolsResponse:
    """District approval queue (SC-069). Read-only — no transaction to commit/roll back."""
    return schools_svc.list_pending_schools(db, staff)


@router.get(
    "/{school_id}",
    response_model=SchoolDetailResponse,
    responses={
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such school."},
    },
)
def get_school(school_id: uuid.UUID, staff: StaffDep, db: DbDep) -> SchoolDetailResponse:
    """Single-school review view (SC-070)."""
    return schools_svc.get_school_detail(db, staff, school_id)


@router.post(
    "/{school_id}/decision",
    response_model=SchoolDecisionResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Caller is not District/Trust leadership.",
        },
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such school."},
        status.HTTP_409_CONFLICT: {
            "model": DecisionConflictResponse,
            "description": "The school is not currently pending (already decided).",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Decision failed; nothing was written.",
        },
    },
)
def decide_school(
    school_id: uuid.UUID, body: SchoolDecisionRequest, staff: StaffDep, db: DbDep
) -> SchoolDecisionResponse:
    """Approve -> school goes live, whole-school Premium trial ARMED (not started). Reject -> the
    decision is recorded, the school stays not-live. District/Trust leadership only (403 otherwise);
    404 unknown school; 409 if the school is not currently pending."""
    try:
        result = schools_svc.decide_school(db, staff, school_id, body.decision)
    except HTTPException:
        db.rollback()  # no partial write survives a forbidden/not-found/already-decided call
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_02_02_error")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Decision failed") from exc
    db.commit()
    return result
