"""School self-registration surface (FR-02-01).

Public (unauthenticated) — a teacher self-registers before they have any account. Thin router: all
business logic lives in ``src.application.schools.services``. It carries its OWN rate-limit bucket
(``rate_limit_registration``) so this anonymous, internet-facing write cannot spend the auth
budget legitimate staff sign-ins depend on. The create is one ACID transaction — commit on success,
roll back on any error; an unexpected failure surfaces a generic 500 and a ``fr_02_01_error`` log.

Every status this endpoint can return is declared in ``responses`` so the generated OpenAPI client
knows them. Note the shape asymmetry the frontend must type-guard: ``detail`` is an OBJECT for 409
and a plain STRING for 429/500, but FastAPI's built-in 422 is an ARRAY of ``{loc,msg,type}``.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from src.application.schools import services as schools_svc
from src.infrastructure.middlewares.auth_middleware import DbDep
from src.infrastructure.middlewares.ratelimit import rate_limit_registration
from src.schemas.schools import (
    ConflictResponse,
    ErrorResponse,
    RegisterSchool,
    RegisterSchoolResponse,
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
