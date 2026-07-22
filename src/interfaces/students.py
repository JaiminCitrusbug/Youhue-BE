"""Student read endpoint — INFRA-02 isolation demonstrator. Staff-only + school-scoped: a caller
from another school (or a student session) is denied (403), proving cross-tenant isolation."""

import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from src.application import isolation
from src.domain.enums import SessionKind
from src.infrastructure.models.identity import Student
from src.interfaces.deps import DbDep, SessionDep

router = APIRouter(prefix="/students", tags=["students"])


class StudentOut(BaseModel):
    id: uuid.UUID
    display_name: str
    age_band: str
    school_id: uuid.UUID


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: uuid.UUID, sess: SessionDep, db: DbDep) -> StudentOut:
    if sess.kind != SessionKind.staff or sess.school_id is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    student = isolation.get_scoped(db, Student, student_id, sess.school_id)
    if student is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")  # cross-tenant or missing
    isolation.audit(
        db,
        actor_id=sess.subject_id,
        action="student.read",
        target=str(student.id),
        school_id=sess.school_id,
    )
    db.commit()
    return StudentOut(
        id=student.id,
        display_name=student.display_name,
        age_band=student.age_band.value,
        school_id=student.school_id,
    )
