"""Student passwordless sign-in: school code + chosen student -> short-lived, single-active-device
session on the student surface (no staff features)."""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.application.auth import sessions
from src.application.auth.schemas import TokenResponse
from src.config import settings
from src.domain.enums import SchoolStatus, SessionKind, StudentStatus
from src.infrastructure.models.identity import School, Student

_GENERIC_401 = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")


def sign_in(
    db: Session, school_code: str, student_id: uuid.UUID, device_id: str | None = None
) -> TokenResponse:
    school = db.scalar(
        select(School).where(
            School.sign_in_code == school_code, School.status == SchoolStatus.active
        )
    )
    if school is None:
        raise _GENERIC_401
    student = db.scalar(
        select(Student).where(
            Student.id == student_id,
            Student.school_id == school.id,
            Student.status == StudentStatus.active,
        )
    )
    if student is None:
        raise _GENERIC_401
    sess = sessions.create_session(  # single-active-device enforced inside create_session
        db, student.id, SessionKind.student, settings.student_session_ttl_minutes,
        school_id=school.id, device_id=device_id,
    )
    return TokenResponse(access_token=sessions.issue_token(sess))
