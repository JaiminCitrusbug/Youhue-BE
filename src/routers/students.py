"""Student read endpoint (INFRA-02/03) + parental-consent capture (FR-20-06). Staff-only;
cross-school/class denied (403) + audited. Thin router — business logic lives in
src.application.students.services / src.application.compliance.services."""
import uuid

from fastapi import APIRouter, HTTPException

from src.application.compliance import services as compliance_svc
from src.application.students import services as students_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.compliance import ConsentIn, ConsentOut
from src.schemas.students import StudentOut

router = APIRouter(prefix="/students", tags=["students"])


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StudentOut:
    s = students_svc.read_student(db, staff, student_id)
    return StudentOut(
        id=s.id, display_name=s.display_name, age_band=s.age_band.value, school_id=s.school_id
    )


@router.post("/{student_id}/consent", response_model=ConsentOut)
def capture_consent(
    student_id: uuid.UUID, body: ConsentIn, staff: StaffDep, db: DbDep
) -> ConsentOut:
    """FR-20-06: capture verifiable parental consent (SC-088, school-mediated, leadership-only).
    200 { status } on success; 422 on an invalid consent record (pydantic rejects an out-of-enum
    `status` before this body runs); 403 cross-school / wrong role (audited)."""
    try:
        recorded = compliance_svc.capture_consent(db, staff, student_id, body.status)
    except HTTPException:
        db.commit()  # persist the audit-logged denial / attempt before surfacing the error
        raise
    db.commit()
    return ConsentOut(status=recorded)
