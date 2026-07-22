"""Student auth surface (FR-01-02, decision #4).

Split out of the shared `routers.auth` so the student sign-in surface owns its own module and can
evolve without touching staff/admin routes. Passwordless and rate-limited; a student session never
resolves a staff route (enforced by the session-kind guard in auth_middleware). Thin router —
business logic lives in src.application.auth.student."""
from fastapi import APIRouter, Depends

from src.application.auth import student as student_svc
from src.infrastructure.middlewares.auth_middleware import DbDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.student_auth import StudentSession, StudentSignIn

router = APIRouter(prefix="/auth/student", tags=["student-auth"])
_throttle = [Depends(rate_limit)]


@router.post("/sign-in", response_model=StudentSession, dependencies=_throttle)
def student_sign_in(body: StudentSignIn, db: DbDep) -> StudentSession:
    result = student_svc.sign_in(
        db,
        school_or_class_code=body.school_or_class_code,
        qr_token=body.qr_token,
        student_id=body.student_id,
        device_id=body.device_id,
    )
    db.commit()
    return result
