"""Session application service: JWT issuance + session lifecycle business rules.

All persistence is delegated to src.domain.auth.services (domain owns DB access).
"""
import uuid
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from config.env_config import settings
from src.constants.enums import SessionKind
from src.domain.auth import services as auth_db
from src.domain.auth.models import AuthSession
from src.utils.security import encode_jwt


def _utcnow() -> datetime:
    return datetime.now(UTC)


def create_session(
    db: Session,
    subject_id: uuid.UUID,
    kind: SessionKind,
    ttl_minutes: int,
    school_id: uuid.UUID | None = None,
    device_id: str | None = None,
    mfa_pending: bool = False,
) -> AuthSession:
    if kind == SessionKind.student and settings.student_single_active_device:
        auth_db.revoke_all_sessions_for_subject(db, subject_id)  # single active device
    return auth_db.create_session_row(
        db,
        subject_id=subject_id,
        kind=kind,
        ttl_minutes=ttl_minutes,
        school_id=school_id,
        device_id=device_id,
        mfa_pending=mfa_pending,
    )


def issue_token(sess: AuthSession) -> str:
    claims = {
        "jti": str(sess.id),
        "sub": str(sess.subject_id),
        "kind": sess.kind.value,
        "school_id": str(sess.school_id) if sess.school_id else None,
        "mfa_pending": sess.mfa_pending,
    }
    return encode_jwt(claims, sess.expires_at)


def get_active_session(db: Session, jti: uuid.UUID) -> AuthSession | None:
    sess = auth_db.get_session(db, jti)
    if sess is None or sess.revoked_at is not None or sess.expires_at <= _utcnow():
        return None
    return sess


def revoke_session(db: Session, jti: uuid.UUID) -> None:
    auth_db.revoke_session_row(db, jti)


def revoke_all_for_subject(db: Session, subject_id: uuid.UUID) -> None:
    auth_db.revoke_all_sessions_for_subject(db, subject_id)


def promote_after_mfa(db: Session, sess: AuthSession, ttl_minutes: int) -> None:
    auth_db.promote_session(db, sess, ttl_minutes)
