"""Student passwordless sign-in business logic (FR-01-02).

A student reaches their check-in with no email/password. Sign-in resolves the class/school from
one of two mutually-exclusive inputs — a `school_or_class_code` OR a class `qr_token` — then the
student picks their name/avatar (`student_id`) within that class/school and is issued a
short-lived, single-active-device session on the student surface (never a staff surface).

Errors are surfaced per the ticket contract: 400 invalid/expired code (or bad input),
404 student not in class/school. DB access is via domain services (backend.md layering).
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import SessionKind
from src.domain.auth import services as auth_db
from src.domain.identity import services as identity_db
from src.domain.identity.models import Student
from src.domain.org import services as org_db
from src.schemas.student_auth import StudentSession

_BAD_CODE = HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired code")
_NOT_IN_CLASS = HTTPException(status.HTTP_404_NOT_FOUND, "Student not in class")
_REPLAYED_QR = HTTPException(
    status.HTTP_400_BAD_REQUEST, "qr_token already used for an active session"
)


def sign_in(
    db: Session,
    *,
    school_or_class_code: str | None,
    qr_token: str | None,
    student_id: uuid.UUID,
    device_id: str | None = None,
) -> StudentSession:
    # qr_token is single-use and MUTUALLY EXCLUSIVE with the code (ticket §Must-nots): exactly one.
    if bool(school_or_class_code) == bool(qr_token):
        raise _BAD_CODE
    if qr_token:
        return _sign_in_by_qr(db, qr_token, student_id, device_id)
    if school_or_class_code:
        return _sign_in_by_code(db, school_or_class_code, student_id, device_id)
    raise _BAD_CODE  # pragma: no cover  (unreachable: exclusivity guard ensures one input)


def _issue(
    db: Session, student: Student, *, qr_token: str | None, device_id: str | None
) -> StudentSession:
    sess = sessions.create_session(  # single-active-device enforced inside create_session
        db,
        student.id,
        SessionKind.student,
        settings.student_session_ttl_minutes,
        school_id=student.school_id,
        device_id=device_id,
        qr_token=qr_token,
    )
    return StudentSession(
        session_token=sessions.issue_token(sess),
        student_id=student.id,
        age_band=student.age_band.value,
    )


def _sign_in_by_code(
    db: Session, code: str, student_id: uuid.UUID, device_id: str | None
) -> StudentSession:
    # A per-class join code narrows to one class; a school code admits any student in the school.
    klass = org_db.get_class_by_join_code(db, code)
    if klass is not None:
        student = identity_db.get_active_student_in_school(db, student_id, klass.school_id)
        if student is None or not org_db.student_in_class(db, student.id, klass.id):
            raise _NOT_IN_CLASS
        return _issue(db, student, qr_token=None, device_id=device_id)

    school = identity_db.get_active_school_by_code(db, code)
    if school is None:
        raise _BAD_CODE
    student = identity_db.get_active_student_in_school(db, student_id, school.id)
    if student is None:
        raise _NOT_IN_CLASS
    return _issue(db, student, qr_token=None, device_id=device_id)


def _sign_in_by_qr(
    db: Session, qr_token: str, student_id: uuid.UUID, device_id: str | None
) -> StudentSession:
    klass = org_db.get_class_by_qr_token(db, qr_token)
    if klass is None:
        raise _BAD_CODE
    student = identity_db.get_active_student_in_school(db, student_id, klass.school_id)
    if student is None or not org_db.student_in_class(db, student.id, klass.id):
        raise _NOT_IN_CLASS
    # Replay protection is per student-session (decision #2): the whole class shares one persistent
    # QR, but a student who already holds an active session from this token cannot re-consume it.
    if auth_db.active_session_by_qr_token(db, student.id, qr_token) is not None:
        raise _REPLAYED_QR
    return _issue(db, student, qr_token=qr_token, device_id=device_id)
