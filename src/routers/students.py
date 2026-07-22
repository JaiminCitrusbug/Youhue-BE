"""Student read endpoint (INFRA-02/03). Staff-only; cross-school/class denied (403) + audited.
Thin router — business logic lives in src.application.students.services."""
import uuid

from fastapi import APIRouter

from src.application.students import services as students_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.students import StudentOut

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StudentOut:
    s = students_svc.read_student(db, staff, student_id)
    return StudentOut(
        id=s.id, display_name=s.display_name, age_band=s.age_band.value, school_id=s.school_id
    )
