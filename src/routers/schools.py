"""School self-registration surface (FR-02-01).

Public (unauthenticated) — a teacher self-registers before they have any account. Thin router: all
business logic lives in ``src.application.schools.services``. Rate-limited like the other write
sign-in/registration surfaces. The create is one ACID transaction — commit on success, roll back on
any error; an unexpected failure surfaces a generic 500 and a ``fr_02_01_error`` log.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, status

from src.application.schools import services as schools_svc
from src.infrastructure.middlewares.auth_middleware import DbDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.schools import RegisterSchool, RegisterSchoolResponse

logger = logging.getLogger("youhue.schools")
router = APIRouter(prefix="/schools", tags=["schools"])
_throttle = [Depends(rate_limit)]


@router.post(
    "",
    response_model=RegisterSchoolResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=_throttle,
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
