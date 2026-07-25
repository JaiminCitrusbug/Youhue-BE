"""School-scoped isolation layer (INFRA-02) + immutable audit helper. Business logic; DB via domain.

`get_scoped` returns a row only when it belongs to the caller's school — cross-tenant access is
impossible by construction. Student-child tables (no own school_id) use `get_scoped_via_student`.

`audit()` is the ONE choke point every feature's immutable audit write goes through (INFRA-02's
`AuditLog` table); FR-20-07 (cross-tenant isolation re-verification) hangs its
`fr_20_07_audit` structured log here rather than duplicating it per-feature — every call to
`audit()`, regardless of caller, is by definition "an immutable audit of a cross-tenant-relevant
action" (BR-12), so logging at this single real choke point covers all of them without touching
any feature's own scoping/rejection logic.
"""
import logging
import uuid
from typing import TypeVar

from sqlalchemy.orm import Session

from src.domain.compliance import services as compliance_db

T = TypeVar("T")

logger = logging.getLogger("youhue.isolation")


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
    logger.info(
        "fr_20_07_audit action=%s actor_id=%s target=%s school_id=%s",
        action, actor_id, target, school_id,
    )
