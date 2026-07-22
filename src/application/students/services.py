"""Student read business logic — school+class authorization + audit (INFRA-02/03). DB via domain."""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.application.isolation import services as isolation
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount, Student


def _audit(db: Session, staff: StaffAccount, action: str, target: str) -> None:
    isolation.audit(db, actor_id=staff.id, action=action, target=target, school_id=staff.school_id)
    db.commit()


def read_student(db: Session, staff: StaffAccount, student_id: uuid.UUID) -> Student:
    student = identity_db.get_student(db, student_id)
    if student is None:
        _audit(db, staff, "student.read.denied", str(student_id))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")  # hide existence
    try:
        authz.require_student_access(db, staff, student)  # school + class scope
    except HTTPException:
        _audit(db, staff, "student.read.denied", str(student.id))
        raise
    _audit(db, staff, "student.read", str(student.id))
    return student
