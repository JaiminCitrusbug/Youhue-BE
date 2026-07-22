"""Staff/shared auth endpoints (INFRA-01). The student surface lives in `routers.student_auth`
(decision #4); a student session never resolves a staff route. Sign-in errors are generic;
forgot-password is 202. Sign-in/OTP/forgot are throttled. Thin router — business logic lives in
src.application.*"""
from fastapi import APIRouter, Depends, HTTPException, Response, status

from src.application.auth import sessions
from src.application.auth import sso as sso_svc
from src.application.auth import staff as staff_svc
from src.application.me import services as me_svc
from src.infrastructure.middlewares.auth_middleware import AnySessionDep, DbDep, SessionDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.auth import (
    ForgotPassword,
    MeResponse,
    MfaVerify,
    ResetPassword,
    StaffSignIn,
    TokenResponse,
)

router = APIRouter(prefix="/auth", tags=["auth"])
_throttle = [Depends(rate_limit)]


@router.post("/staff/sign-in", response_model=TokenResponse, dependencies=_throttle)
def staff_sign_in(body: StaffSignIn, db: DbDep) -> TokenResponse:
    result = staff_svc.sign_in(db, body.email, body.password, body.device_id)
    db.commit()
    return result


@router.post("/staff/mfa/verify", response_model=TokenResponse, dependencies=_throttle)
def staff_mfa_verify(body: MfaVerify, db: DbDep) -> TokenResponse:
    try:
        result = staff_svc.verify_mfa_and_promote(db, body.session_token, body.code)
    except HTTPException:
        db.commit()  # persist any failed-attempt record (brute-force cap)
        raise
    db.commit()
    return result


@router.post("/staff/forgot-password", status_code=status.HTTP_202_ACCEPTED, dependencies=_throttle)
def forgot_password(body: ForgotPassword, db: DbDep) -> dict[str, str]:
    staff_svc.forgot_password(db, body.email)
    db.commit()
    return {"status": "accepted"}


@router.post("/staff/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(body: ResetPassword, db: DbDep) -> Response:
    staff_svc.reset_password(db, body.token, body.new_password)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/staff/sso/{provider}")
def sso_start(provider: str) -> dict[str, str]:
    sso_svc.require_enabled(provider)
    return {"provider": provider, "status": "configured"}  # pragma: no cover


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(sess: AnySessionDep, db: DbDep) -> Response:
    sessions.revoke_session(db, sess.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


me_router = APIRouter(tags=["auth"])


@me_router.get("/me", response_model=MeResponse)
def me(sess: SessionDep, db: DbDep) -> MeResponse:
    return me_svc.resolve_me(db, sess)
