"""Student read endpoint — isolation + role/class scoping demonstrator (INFRA-02/03). Staff-only;
a caller from another school, or a teacher outside the student's class, is denied (403). Every
access — allowed OR denied — is written to the immutable audit trail (intrusion signal)."""
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from src.application import authz, isolation
from src.infrastructure.models.identity import StaffAccount, Student
from src.interfaces.deps import DbDep, StaffDep

router = APIRouter(prefix="/students", tags=["students"])


class StudentOut(BaseModel):
    id: uuid.UUID
    display_name: str
    age_band: str
    school_id: uuid.UUID


def _audit(db: Session, staff: StaffAccount, action: str, target: str) -> None:
    isolation.audit(db, actor_id=staff.id, action=action, target=target, school_id=staff.school_id)
    db.commit()


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StudentOut:
    student = db.get(Student, student_id)
    if student is None:
        _audit(db, staff, "student.read.denied", str(student_id))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")  # hide existence
    try:
        authz.require_student_access(db, staff, student)  # school + class scope
    except HTTPException:
        _audit(db, staff, "student.read.denied", str(student.id))
        raise
    _audit(db, staff, "student.read", str(student.id))
    return StudentOut(
        id=student.id,
        display_name=student.display_name,
        age_band=student.age_band.value,
        school_id=student.school_id,
    )
