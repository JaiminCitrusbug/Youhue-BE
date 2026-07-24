"""Auth dependencies + the cross-tenant authorization guard (FR-01-07).

The guard is defined here (INFRA-01) and enforced against the school-scoped model (INFRA-02):
every protected route checks session.school_id == resource.school_id BEFORE returning data,
and is tested as the disallowed actor (403).
"""
import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from config.db_connection import get_db
from src.application.auth import sessions
from src.constants.enums import SessionKind, StaffStatus, StudentStatus
from src.domain.auth.models import AuthSession
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount, Student
from src.utils.security import decode_jwt

_bearer = HTTPBearer(auto_error=True)
DbDep = Annotated[Session, Depends(get_db)]


def _load_session(db: Session, token: str, *, allow_mfa_pending: bool) -> AuthSession:
    try:
        payload = decode_jwt(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    jti = payload.get("jti")
    try:
        sess = sessions.get_active_session(db, uuid.UUID(str(jti))) if jti else None
    except (ValueError, TypeError):
        sess = None  # malformed jti in a validly-signed token -> treat as no session (401)
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or revoked")
    if sess.mfa_pending and not allow_mfa_pending:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA verification required")
    return sess


def get_current_session(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)], db: DbDep
) -> AuthSession:
    return _load_session(db, creds.credentials, allow_mfa_pending=False)


def get_session_allow_mfa(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)], db: DbDep
) -> AuthSession:
    return _load_session(db, creds.credentials, allow_mfa_pending=True)


SessionDep = Annotated[AuthSession, Depends(get_current_session)]
AnySessionDep = Annotated[AuthSession, Depends(get_session_allow_mfa)]


def get_current_staff(sess: SessionDep, db: DbDep) -> StaffAccount:
    if sess.kind != SessionKind.staff:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    staff = db.get(StaffAccount, sess.subject_id)
    if staff is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No account")
    # A staff member acts only from an ACTIVE account at a LIVE school. Every staff route hangs off
    # StaffDep, so this is the one choke point that makes "pending" mean pending on the staff
    # surface too: a school awaiting District approval (FR-02-01) or rejected (FR-02-02) can have
    # no side effect, whatever minted the session and however old it is. Checked per request, so
    # revoking a school or deactivating an account takes effect immediately, not at token expiry.
    if staff.status != StaffStatus.active or not identity_db.school_is_active(db, staff.school_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    return staff


StaffDep = Annotated[StaffAccount, Depends(get_current_staff)]


def get_current_student(sess: SessionDep, db: DbDep) -> Student:
    """FR-04-01: resolves the CALLER's own student account off the session's `subject_id` —
    never a request param, so a student route can never act on behalf of anyone else (mirrors
    `get_current_staff` above, applied to the student surface)."""
    if sess.kind != SessionKind.student:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    student = db.get(Student, sess.subject_id)
    if student is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No account")
    # Same "checked per request, not just at token mint" posture as `get_current_staff`: a
    # deactivated student or a school that stops being live takes effect immediately.
    if student.status != StudentStatus.active or not identity_db.school_is_active(
        db, student.school_id
    ):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    return student


StudentDep = Annotated[Student, Depends(get_current_student)]


def require_same_school(sess: AuthSession, resource_school_id: uuid.UUID) -> None:
    """The cross-tenant guard: deny (403) before returning another school's data."""
    if sess.school_id is None or sess.school_id != resource_school_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
