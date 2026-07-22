"""Student read endpoint — isolation + role/class scoping demonstrator (INFRA-02/03). Staff-only;
a caller from another school, or a teacher outside the student's class, is denied (403)."""
import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.application import authz, isolation
from src.infrastructure.models.identity import Student
from src.interfaces.deps import DbDep, StaffDep

router = APIRouter(prefix="/students", tags=["students"])


class StudentOut(BaseModel):
    id: uuid.UUID
    display_name: str
    age_band: str
    school_id: uuid.UUID


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StudentOut:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")  # hide existence
    authz.require_student_access(db, staff, student)  # school + class scope -> 403
    isolation.audit(
        db, actor_id=staff.id, action="student.read",
        target=str(student.id), school_id=staff.school_id,
    )
    db.commit()
    return StudentOut(
        id=student.id, display_name=student.display_name,
        age_band=student.age_band.value, school_id=student.school_id,
    )
