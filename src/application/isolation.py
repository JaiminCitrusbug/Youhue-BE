"""School-scoped isolation query layer (INFRA-02) + immutable audit helper.

Every school-scoped resource load goes through `get_scoped`, which returns the row only when it
belongs to the caller's school — cross-tenant access is impossible by construction (one school's
data is never returned to another). `audit` writes the immutable AuditLog trail.
"""
import uuid
from typing import TypeVar

from sqlalchemy.orm import Session

from src.infrastructure.models.compliance import AuditLog
from src.infrastructure.models.identity import Student

T = TypeVar("T")


def get_scoped(db: Session, model: type[T], obj_id: uuid.UUID, school_id: uuid.UUID) -> T | None:
    """Load a row by id ONLY if it has a matching `school_id`; otherwise None (caller denies).

    NOTE: use this only for models that carry `school_id` directly. Student-child tables
    (SupportiveNote, ParentalConsent, ActivityEngagement, ...) have no `school_id` and would
    always fail-closed here — isolate those with `get_scoped_via_student` instead.
    """
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "school_id", None) != school_id:
        return None
    return obj


def get_scoped_via_student(
    db: Session, model: type[T], obj_id: uuid.UUID, school_id: uuid.UUID
) -> T | None:
    """Isolate a student-child row (no own `school_id`) via its student's school (M2 fix)."""
    obj = db.get(model, obj_id)
    if obj is None:
        return None
    student_id = getattr(obj, "student_id", None)
    if student_id is None:
        return None
    student = db.get(Student, student_id)
    if student is None or student.school_id != school_id:
        return None
    return obj


def audit(
    db: Session,
    *,
    actor_id: uuid.UUID,
    action: str,
    target: str,
    school_id: uuid.UUID | None = None,
) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, target=target, school_id=school_id))
    db.flush()
