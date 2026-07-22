"""Admin console sign-in business logic (FR-19-01) — its own module (decision #4), disjoint from
the staff/student surfaces.

Flow (single POST /admin/sign-in, email + password + optional mfa_code):
  1. Verify email + password. Errors are GENERIC (401, no account-existence disclosure).
  2. No `mfa_code` yet -> issue an email OTP + return `mfa_required` (the AdminMfaChallenge step).
     MFA is ALWAYS required to complete admin sign-in.
  3. `mfa_code` present -> re-verify email + password, verify the OTP, then mint the admin session
     + return the role. A wrong OTP is the same generic 401 (no factor disclosure).

Reuses the existing email-OTP `MfaOtp` machinery (via the shared domain services) — no TOTP, no
external dependency. Rate-limiting is applied at the router; account lockout + an OTP brute-force
cap are applied here.
"""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.auth import lockout, sessions
from src.constants.enums import SessionKind
from src.domain.auth import services as auth_db
from src.domain.compliance import services as audit_db
from src.domain.identity import services as identity_db
from src.infrastructure.emailer import send_email
from src.schemas.admin import AdminSignInResponse
from src.utils import security

_GENERIC_401 = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")


def sign_in(
    db: Session,
    email: str,
    password: str,
    mfa_code: str | None = None,
    device_id: str | None = None,
) -> AdminSignInResponse:
    ident = f"admin:{email.lower()}"
    if lockout.is_locked(db, ident):
        raise HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked")

    admin = identity_db.get_admin_by_email(db, email.lower())
    # Always run bcrypt against a real-or-dummy hash -> constant time, no user-enumeration leak.
    target_hash = (
        admin.password_hash if admin and admin.password_hash else security.DUMMY_PASSWORD_HASH
    )
    password_ok = security.verify_password(password, target_hash)
    if admin is None or admin.password_hash is None or not password_ok:
        lockout.record_attempt(db, ident, succeeded=False)
        raise _GENERIC_401
    lockout.record_attempt(db, ident, succeeded=True)  # valid password resets the failure streak

    if mfa_code is None:
        _issue_mfa_otp(db, admin.id, admin.email)  # AdminMfaChallenge: OTP emailed
        return AdminSignInResponse(mfa_required=True)

    if not _verify_otp(db, admin.id, mfa_code):
        raise _GENERIC_401  # generic — never disclose which factor failed

    sess = sessions.create_session(
        db,
        admin.id,
        SessionKind.admin,
        settings.admin_session_ttl_minutes,
        school_id=None,  # admin is platform-level, never school-scoped
        device_id=device_id,
    )
    audit_db.write_audit(
        db, actor_id=admin.id, action="admin.sign_in", target="admin_console", school_id=None
    )
    return AdminSignInResponse(
        admin_session=sessions.issue_token(sess), role=admin.role.value
    )


def _issue_mfa_otp(db: Session, subject_id: uuid.UUID, email: str) -> None:
    code = security.new_numeric_code(settings.mfa_otp_length)
    auth_db.add_otp(
        db,
        subject_id,
        security.hash_secret(code),
        datetime.now(UTC) + timedelta(minutes=settings.mfa_otp_ttl_minutes),
    )
    send_email(email, "Your Youhue admin verification code", f"Your verification code is {code}")


def _verify_otp(db: Session, subject_id: uuid.UUID, code: str) -> bool:
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
