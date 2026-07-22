"""Compliance domain services — audit persistence + scoped loads (DB access only)."""
import uuid
from typing import TypeVar

from sqlalchemy.orm import Session

from src.domain.compliance.models import AuditLog
from src.domain.identity.models import Student

T = TypeVar("T")


def write_audit(
    db: Session,
    *,
    actor_id: uuid.UUID,
    action: str,
    target: str,
    school_id: uuid.UUID | None,
) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, target=target, school_id=school_id))
    db.flush()


def get_by_id(db: Session, model: type[T], obj_id: uuid.UUID) -> T | None:
    return db.get(model, obj_id)


def get_student(db: Session, student_id: uuid.UUID) -> Student | None:
    return db.get(Student, student_id)
