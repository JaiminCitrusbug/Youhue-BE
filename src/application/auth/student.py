"""Student passwordless sign-in business logic: school code + chosen student -> short-lived,
single-active-device session on the student surface (no staff features). DB via domain services."""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import SessionKind
from src.domain.identity import services as identity_db
from src.schemas.auth import TokenResponse

_GENERIC_401 = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")


def sign_in(
    db: Session, school_code: str, student_id: uuid.UUID, device_id: str | None = None
) -> TokenResponse:
    school = identity_db.get_active_school_by_code(db, school_code)
    if school is None:
        raise _GENERIC_401
    student = identity_db.get_active_student_in_school(db, student_id, school.id)
    if student is None:
        raise _GENERIC_401
    sess = sessions.create_session(  # single-active-device enforced inside create_session
        db, student.id, SessionKind.student, settings.student_session_ttl_minutes,
        school_id=school.id, device_id=device_id,
    )
    return TokenResponse(access_token=sessions.issue_token(sess))
