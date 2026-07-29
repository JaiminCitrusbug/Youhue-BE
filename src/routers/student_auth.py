"""Student auth surface (FR-01-02, decision #4).

Split out of the shared `routers.auth` so the student sign-in surface owns its own module and can
evolve without touching staff/admin routes. Passwordless and rate-limited; a student session never
resolves a staff route (enforced by the session-kind guard in auth_middleware). Thin router —
business logic lives in src.application.auth.student."""
from fastapi import APIRouter, Depends, HTTPException, Request

from src.application.auth import student as student_svc
from src.infrastructure.middlewares.auth_middleware import DbDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.student_auth import RosterEntryOut, RosterListOut, StudentSession, StudentSignIn

router = APIRouter(prefix="/auth/student", tags=["student-auth"])
_throttle = [Depends(rate_limit)]


@router.post("/sign-in", response_model=StudentSession, dependencies=_throttle)
def student_sign_in(body: StudentSignIn, request: Request, db: DbDep) -> StudentSession:
    client_ip = request.client.host if request.client else None
    try:
        result = student_svc.sign_in(
            db,
            school_or_class_code=body.school_or_class_code,
            qr_token=body.qr_token,
            student_id=body.student_id,
            device_id=body.device_id,
            client_ip=client_ip,
        )
    except HTTPException:
        db.commit()  # persist the failed-attempt record (escalation counter) before surfacing
        raise
    db.commit()
    return result


@router.get("/roster", response_model=RosterListOut, dependencies=_throttle)
def student_roster(
    db: DbDep, school_or_class_code: str | None = None, qr_token: str | None = None
) -> RosterListOut:
    """The real name/avatar list for the SC-021 picker screen. Public (pre-auth) by design, same
    as sign-in itself — the code/QR is this flow's one shared credential; see
    `student_svc.list_roster_for_code`'s docstring for why this doesn't widen disclosure. Reuses
    the SAME 400 on an unresolvable code/token as sign-in, for a consistent error surface."""
    students = student_svc.list_roster_for_code(
        db, school_or_class_code=school_or_class_code, qr_token=qr_token
    )
    return RosterListOut(
        students=[RosterEntryOut(id=s.id, display_name=s.display_name) for s in students]
    )
