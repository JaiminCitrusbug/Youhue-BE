"""School-scoped isolation layer (INFRA-02) + immutable audit helper. Business logic; DB via domain.

`get_scoped` returns a row only when it belongs to the caller's school — cross-tenant access is
impossible by construction. Student-child tables (no own school_id) use `get_scoped_via_student`.
"""
import uuid
from typing import TypeVar

from sqlalchemy.orm import Session

from src.domain.compliance import services as compliance_db

T = TypeVar("T")


def get_scoped(db: Session, model: type[T], obj_id: uuid.UUID, school_id: uuid.UUID) -> T | None:
    obj = compliance_db.get_by_id(db, model, obj_id)
    if obj is None or getattr(obj, "school_id", None) != school_id:
        return None
    return obj


def get_scoped_via_student(
    db: Session, model: type[T], obj_id: uuid.UUID, school_id: uuid.UUID
) -> T | None:
    obj = compliance_db.get_by_id(db, model, obj_id)
    if obj is None:
        return None
    student_id = getattr(obj, "student_id", None)
    if student_id is None:
        return None
    student = compliance_db.get_student(db, student_id)
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
    compliance_db.write_audit(
        db, actor_id=actor_id, action=action, target=target, school_id=school_id
    )
