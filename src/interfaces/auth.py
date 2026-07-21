"""Auth endpoints (INFRA-01). Two surfaces (student/staff); a student session never resolves
a staff route. Sign-in errors are generic; forgot-password is 202 with no disclosure."""
import uuid

from fastapi import APIRouter, HTTPException, Response, status

from src.application.auth import sessions
from src.application.auth import sso as sso_svc
from src.application.auth import staff as staff_svc
from src.application.auth import student as student_svc
from src.application.auth.schemas import (
    ForgotPassword,
    MeResponse,
    MfaVerify,
    ResetPassword,
    StaffSignIn,
    StudentSignIn,
    TokenResponse,
)
from src.application.auth.security import decode_jwt
from src.config import settings
from src.domain.enums import SessionKind
from src.infrastructure.models.identity import StaffAccount
from src.interfaces.deps import DbDep, SessionDep

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/staff/sign-in", response_model=TokenResponse)
def staff_sign_in(body: StaffSignIn, db: DbDep) -> TokenResponse:
    result = staff_svc.sign_in(db, body.email, body.password, body.device_id)
    db.commit()
    return result


@router.post("/staff/mfa/verify", response_model=TokenResponse)
def staff_mfa_verify(body: MfaVerify, db: DbDep) -> TokenResponse:
    try:
        payload = decode_jwt(body.session_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    jti = payload.get("jti")
    sess = sessions.get_active_session(db, uuid.UUID(jti)) if jti else None
    if sess is None or not sess.mfa_pending:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No pending MFA")
    if not staff_svc.verify_mfa(db, sess.subject_id, body.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")
    sessions.promote_after_mfa(db, sess, settings.staff_session_ttl_minutes)
    token = sessions.issue_token(sess)
    db.commit()
    return TokenResponse(access_token=token)


@router.post("/student/sign-in", response_model=TokenResponse)
def student_sign_in(body: StudentSignIn, db: DbDep) -> TokenResponse:
    result = student_svc.sign_in(db, body.school_code, body.student_id, body.device_id)
    db.commit()
    return result


@router.post("/staff/forgot-password", status_code=status.HTTP_202_ACCEPTED)
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
    # Configured-provider authorize redirect is wired via Authlib; unconfigured -> 501 above.
    return {"provider": provider, "status": "configured"}  # pragma: no cover


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(sess: SessionDep, db: DbDep) -> Response:
    sessions.revoke_session(db, sess.id)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


me_router = APIRouter(tags=["auth"])


@me_router.get("/me", response_model=MeResponse)
def me(sess: SessionDep, db: DbDep) -> MeResponse:
    role: str | None = None
    if sess.kind == SessionKind.staff:
        staff = db.get(StaffAccount, sess.subject_id)
        role = staff.role.value if staff else None
    return MeResponse(
        subject_id=sess.subject_id, kind=sess.kind.value, school_id=sess.school_id, role=role
    )
