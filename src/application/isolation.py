"""School-scoped isolation query layer (INFRA-02) + immutable audit helper.

Every school-scoped resource load goes through `get_scoped`, which returns the row only when it
belongs to the caller's school — cross-tenant access is impossible by construction (one school's
data is never returned to another). `audit` writes the immutable AuditLog trail.
"""
import uuid
from typing import TypeVar

from sqlalchemy.orm import Session

from src.infrastructure.models.compliance import AuditLog

T = TypeVar("T")


def get_scoped(db: Session, model: type[T], obj_id: uuid.UUID, school_id: uuid.UUID) -> T | None:
    """Load a row by id ONLY if it belongs to `school_id`; otherwise None (caller denies)."""
    obj = db.get(model, obj_id)
    if obj is None or getattr(obj, "school_id", None) != school_id:
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
