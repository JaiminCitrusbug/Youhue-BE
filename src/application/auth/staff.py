"""Staff auth business logic: sign-in, email-OTP MFA, single-use expiring reset.

Generic errors only — a sign-in failure never reveals whether an email is registered (401);
forgot-password always returns success (202). All DB access is via domain services.

A correct credential is necessary but not sufficient: the account's SCHOOL must also be live. A
school self-registered under FR-02-01 is pending until a District/Trust admin approves it, and a
pending (or rejected) school issues no staff session at all — see ``_SCHOOL_NOT_ACTIVE``.
"""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.auth import lockout, sessions
from src.constants.enums import SessionKind
from src.domain.auth import services as auth_db
from src.domain.identity import services as identity_db
from src.infrastructure.emailer import send_email
from src.schemas.auth import TokenResponse
from src.utils import security

_GENERIC_401 = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")
# Raised only AFTER the credential verified, so it discloses nothing to anyone who is not already
# the account holder — no enumeration value, and the holder needs to be told why they are stuck.
_SCHOOL_NOT_ACTIVE = HTTPException(
    status.HTTP_403_FORBIDDEN, "School is not active yet — it is awaiting District approval"
)


def sign_in(db: Session, email: str, password: str, device_id: str | None = None) -> TokenResponse:
    ident = email.lower()
    if lockout.is_locked(db, ident):
        raise HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked")
    staff = identity_db.get_active_staff_by_email(db, ident)
    # Always run bcrypt (dummy hash if no password account) -> constant time, no user-enumeration.
    target_hash = (
        staff.password_hash if staff and staff.password_hash else security.DUMMY_PASSWORD_HASH
    )
    password_ok = security.verify_password(password, target_hash)
    if staff is None or staff.password_hash is None or not password_ok:
        lockout.record_attempt(db, ident, succeeded=False)
        raise _GENERIC_401
    lockout.record_attempt(db, ident, succeeded=True)
    if not identity_db.school_is_active(db, staff.school_id):
        raise _SCHOOL_NOT_ACTIVE  # pending/rejected school -> no session is minted at all

    if staff.mfa_enabled or staff.role.value in settings.mfa_roles:  # forced for privileged roles
        sess = sessions.create_session(
            db, staff.id, SessionKind.staff, settings.mfa_otp_ttl_minutes,
            school_id=staff.school_id, device_id=device_id, mfa_pending=True,
        )
        _issue_mfa_otp(db, staff.id, staff.email)
        return TokenResponse(access_token=sessions.issue_token(sess), mfa_required=True)

    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id, device_id=device_id,
    )
    return TokenResponse(access_token=sessions.issue_token(sess))


def _issue_mfa_otp(db: Session, subject_id: uuid.UUID, email: str) -> None:
    code = security.new_numeric_code(settings.mfa_otp_length)
    auth_db.add_otp(
        db, subject_id, security.hash_secret(code),
        datetime.now(UTC) + timedelta(minutes=settings.mfa_otp_ttl_minutes),
    )
    send_email(email, "Your Youhue verification code", f"Your verification code is {code}")


def verify_mfa(db: Session, subject_id: uuid.UUID, code: str) -> bool:
    mfa_ident = f"mfa:{subject_id}"
    if lockout.is_locked(db, mfa_ident, settings.mfa_max_attempts):
        return False  # too many wrong OTP guesses -> brute-force cap
    otp = auth_db.latest_unconsumed_otp(db, subject_id)
    if (
        otp is None
        or otp.expires_at <= datetime.now(UTC)
        or otp.code_hash != security.hash_secret(code)
    ):
        lockout.record_attempt(db, mfa_ident, succeeded=False)
        return False
    auth_db.consume_otp(db, otp)
    lockout.record_attempt(db, mfa_ident, succeeded=True)
    return True


def verify_mfa_and_promote(db: Session, session_token: str, code: str) -> TokenResponse:
    """Resolve the pending-MFA session from its token, verify the OTP, promote to a full session."""
    try:
        payload = security.decode_jwt(session_token)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    jti = payload.get("jti")
    try:
        sess = sessions.get_active_session(db, uuid.UUID(str(jti))) if jti else None
    except (ValueError, TypeError):
        sess = None
    if sess is None or not sess.mfa_pending:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No pending MFA")
    if not verify_mfa(db, sess.subject_id, code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid code")
    sessions.promote_after_mfa(db, sess, settings.staff_session_ttl_minutes)
    return TokenResponse(access_token=sessions.issue_token(sess))


def forgot_password(db: Session, email: str) -> None:
    """Always succeeds to the caller (202). Only a real password account gets a reset link."""
    staff = identity_db.get_active_staff_by_email(db, email)
    if staff is None or staff.password_hash is None:
        return  # SSO-only or unknown email -> no disclosure, no email
    raw = security.new_url_token()
    auth_db.add_reset_token(
        db, staff.id, security.hash_secret(raw), datetime.now(UTC) + timedelta(hours=1)
    )
    link = f"{settings.frontend_base_url}/reset-password?token={raw}"
    send_email(staff.email, "Reset your Youhue password", f"Reset link: {link}")


def reset_password(db: Session, token: str, new_password: str) -> None:
    row = auth_db.get_unconsumed_reset_token(db, security.hash_secret(token))
    if row is None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token")
    staff = identity_db.get_staff(db, row.staff_id)
    if staff is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token")
    auth_db.consume_reset_and_set_password(db, row, staff, security.hash_password(new_password))
    sessions.revoke_all_for_subject(db, staff.id)  # revoke-on-password-change
