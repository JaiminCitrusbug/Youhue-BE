"""Staff auth: email/password sign-in, email-OTP MFA, single-use expiring password reset.

Generic errors only — a sign-in failure never reveals whether an email is registered (401);
forgot-password always returns success (202) with no account-existence disclosure.
"""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.application.auth import lockout, security, sessions
from src.application.auth.schemas import TokenResponse
from src.config import settings
from src.domain.enums import SessionKind, StaffStatus
from src.infrastructure.email import send_email
from src.infrastructure.models.auth import MfaOtp, PasswordResetToken
from src.infrastructure.models.identity import StaffAccount

_GENERIC_401 = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")


def _find_active_staff_by_email(db: Session, email: str) -> StaffAccount | None:
    return db.scalar(
        select(StaffAccount).where(
            StaffAccount.email == email.lower(), StaffAccount.status == StaffStatus.active
        )
    )


def sign_in(db: Session, email: str, password: str, device_id: str | None = None) -> TokenResponse:
    ident = email.lower()
    if lockout.is_locked(db, ident):
        raise HTTPException(status.HTTP_423_LOCKED, "Account temporarily locked")
    staff = _find_active_staff_by_email(db, ident)
    if (
        staff is None
        or staff.password_hash is None
        or not security.verify_password(password, staff.password_hash)
    ):
        lockout.record_attempt(db, ident, succeeded=False)
        raise _GENERIC_401
    lockout.record_attempt(db, ident, succeeded=True)

    if staff.mfa_enabled:
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
    db.add(
        MfaOtp(
            subject_id=subject_id,
            code_hash=security.hash_secret(code),
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.mfa_otp_ttl_minutes),
        )
    )
    db.flush()
    send_email(email, "Your Youhue verification code", f"Your verification code is {code}")


def verify_mfa(db: Session, subject_id: uuid.UUID, code: str) -> bool:
    stmt = (
        select(MfaOtp)
        .where(MfaOtp.subject_id == subject_id, MfaOtp.consumed_at.is_(None))
        .order_by(MfaOtp.expires_at.desc())
    )
    otp = db.scalar(stmt)
    if otp is None or otp.expires_at <= datetime.now(UTC):
        return False
    if otp.code_hash != security.hash_secret(code):
        return False
    otp.consumed_at = datetime.now(UTC)
    db.flush()
    return True


def forgot_password(db: Session, email: str) -> None:
    """Always succeeds to the caller (202). Only a real password account gets a reset link."""
    staff = _find_active_staff_by_email(db, email)
    if staff is None or staff.password_hash is None:
        return  # SSO-only or unknown email -> no disclosure, no email
    raw = security.new_url_token()
    db.add(
        PasswordResetToken(
            staff_id=staff.id,
            token_hash=security.hash_secret(raw),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
    )
    db.flush()
    link = f"{settings.oauth_redirect_base}/reset-password?token={raw}"
    send_email(staff.email, "Reset your Youhue password", f"Reset link: {link}")


def reset_password(db: Session, token: str, new_password: str) -> None:
    row = db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == security.hash_secret(token),
            PasswordResetToken.consumed_at.is_(None),
        )
    )
    if row is None or row.expires_at <= datetime.now(UTC):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token")
    staff = db.get(StaffAccount, row.staff_id)
    if staff is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired reset token")
    staff.password_hash = security.hash_password(new_password)
    row.consumed_at = datetime.now(UTC)
    sessions.revoke_all_for_subject(db, staff.id)  # revoke-on-password-change
    db.flush()
