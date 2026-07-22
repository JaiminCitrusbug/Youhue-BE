"""Org domain services — class-access / membership queries only."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain.org.models import ClassGroup, ClassMembership, StaffClassAccess


def get_class_ids_for_staff_in_school(
    db: Session, staff_id: uuid.UUID, school_id: uuid.UUID
) -> list[uuid.UUID]:
    return list(
        db.scalars(
            select(StaffClassAccess.class_id)
            .join(ClassGroup, ClassGroup.id == StaffClassAccess.class_id)
            .where(StaffClassAccess.staff_id == staff_id, ClassGroup.school_id == school_id)
        )
    )


def student_in_any_class(db: Session, student_id: uuid.UUID, class_ids: list[uuid.UUID]) -> bool:
    if not class_ids:
        return False
    member = db.scalar(
        select(ClassMembership).where(
            ClassMembership.student_id == student_id, ClassMembership.class_id.in_(class_ids)
        )
    )
    return member is not None
