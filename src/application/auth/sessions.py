"""DB-backed session store: issuance, JWT, validation, revocation, single-active-device.

Postgres-only (owner decision). The session row is the revocation authority; the JWT carries its id.
"""
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session

from src.application.auth.security import encode_jwt
from src.domain.enums import SessionKind
from src.infrastructure.models.auth import AuthSession


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
    if kind == SessionKind.student:
        # single active device: a new student sign-in ends any prior active student session
        revoke_all_for_subject(db, subject_id)
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
    sess = db.get(AuthSession, jti)
    if sess is None or sess.revoked_at is not None or sess.expires_at <= _utcnow():
        return None
    return sess


def revoke_session(db: Session, jti: uuid.UUID) -> None:
    sess = db.get(AuthSession, jti)
    if sess is not None and sess.revoked_at is None:
        sess.revoked_at = _utcnow()
        db.flush()


def revoke_all_for_subject(db: Session, subject_id: uuid.UUID) -> None:
    db.execute(
        update(AuthSession)
        .where(AuthSession.subject_id == subject_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=_utcnow())
    )
    db.flush()


def promote_after_mfa(db: Session, sess: AuthSession, ttl_minutes: int) -> None:
    sess.mfa_pending = False
    sess.expires_at = _utcnow() + timedelta(minutes=ttl_minutes)
    db.flush()
