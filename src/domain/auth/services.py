"""Auth-infra domain services — sessions, login attempts, OTP, reset tokens (DB access only)."""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from src.constants.enums import SessionKind
from src.domain.auth.models import AuthSession, LoginAttempt, MfaOtp, PasswordResetToken
from src.domain.identity.models import StaffAccount


def _utcnow() -> datetime:
    return datetime.now(UTC)


def create_session_row(
    db: Session,
    *,
    subject_id: uuid.UUID,
    kind: SessionKind,
    ttl_minutes: int,
    school_id: uuid.UUID | None,
    device_id: str | None,
    mfa_pending: bool,
) -> AuthSession:
    sess = AuthSession(
        subject_id=subject_id,
        kind=kind,
        school_id=school_id,
        device_id=device_id,
        mfa_pending=mfa_pending,
        expires_at=_utcnow() + timedelta(minutes=ttl_minutes),
    )
    db.add(sess)
    db.flush()
    return sess


def get_session(db: Session, jti: uuid.UUID) -> AuthSession | None:
    return db.get(AuthSession, jti)


def promote_session(db: Session, sess: AuthSession, ttl_minutes: int) -> None:
    sess.mfa_pending = False
    sess.expires_at = _utcnow() + timedelta(minutes=ttl_minutes)
    db.flush()


def consume_otp(db: Session, otp: MfaOtp) -> None:
    otp.consumed_at = _utcnow()
    db.flush()


def consume_reset_and_set_password(
    db: Session, row: PasswordResetToken, staff: StaffAccount, password_hash: str
) -> None:
    staff.password_hash = password_hash
    row.consumed_at = _utcnow()
    db.flush()


def revoke_session_row(db: Session, jti: uuid.UUID) -> None:
    sess = db.get(AuthSession, jti)
    if sess is not None and sess.revoked_at is None:
        sess.revoked_at = _utcnow()
        db.flush()


def revoke_all_sessions_for_subject(db: Session, subject_id: uuid.UUID) -> None:
    db.execute(
        update(AuthSession)
        .where(AuthSession.subject_id == subject_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_utcnow())
    )
    db.flush()


def add_login_attempt(db: Session, identifier: str, succeeded: bool) -> None:
    db.add(LoginAttempt(identifier=identifier, succeeded=succeeded))
    db.flush()


def recent_attempts(db: Session, identifier: str, window_start: datetime) -> list[LoginAttempt]:
    return list(
        db.scalars(
            select(LoginAttempt)
            .where(LoginAttempt.identifier == identifier, LoginAttempt.at >= window_start)
            .order_by(LoginAttempt.at.desc())
        )
    )


def add_otp(db: Session, subject_id: uuid.UUID, code_hash: str, expires_at: datetime) -> None:
    db.add(MfaOtp(subject_id=subject_id, code_hash=code_hash, expires_at=expires_at))
    db.flush()


def latest_unconsumed_otp(db: Session, subject_id: uuid.UUID) -> MfaOtp | None:
    return db.scalar(
        select(MfaOtp)
        .where(MfaOtp.subject_id == subject_id, MfaOtp.consumed_at.is_(None))
        .order_by(MfaOtp.expires_at.desc())
    )


def add_reset_token(
    db: Session, staff_id: uuid.UUID, token_hash: str, expires_at: datetime
) -> None:
    db.add(PasswordResetToken(staff_id=staff_id, token_hash=token_hash, expires_at=expires_at))
    db.flush()


def get_unconsumed_reset_token(db: Session, token_hash: str) -> PasswordResetToken | None:
    return db.scalar(
        select(PasswordResetToken).where(
            PasswordResetToken.token_hash == token_hash,
            PasswordResetToken.consumed_at.is_(None),
        )
    )
