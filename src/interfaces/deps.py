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

from src.application.auth import sessions
from src.application.auth.security import decode_jwt
from src.domain.enums import SessionKind
from src.infrastructure.db import get_db
from src.infrastructure.models.auth import AuthSession
from src.infrastructure.models.identity import StaffAccount

_bearer = HTTPBearer(auto_error=True)
DbDep = Annotated[Session, Depends(get_db)]


def get_current_session(
    creds: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    db: DbDep,
    allow_mfa_pending: bool = False,
) -> AuthSession:
    try:
        payload = decode_jwt(creds.credentials)
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token") from exc
    jti = payload.get("jti")
    sess = sessions.get_active_session(db, uuid.UUID(jti)) if jti else None
    if sess is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired or revoked")
    if sess.mfa_pending and not allow_mfa_pending:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA verification required")
    return sess


SessionDep = Annotated[AuthSession, Depends(get_current_session)]


def get_current_staff(sess: SessionDep, db: DbDep) -> StaffAccount:
    if sess.kind != SessionKind.staff:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    staff = db.get(StaffAccount, sess.subject_id)
    if staff is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No account")
    return staff


StaffDep = Annotated[StaffAccount, Depends(get_current_staff)]


def require_same_school(sess: AuthSession, resource_school_id: uuid.UUID) -> None:
    """The cross-tenant guard: deny (403) before returning another school's data."""
    if sess.school_id is None or sess.school_id != resource_school_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
