"""Org domain services — class-access / membership queries only."""
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import SchoolStatus, StaffClassScope
from src.domain.identity.models import School
from src.domain.org.models import ClassGroup, ClassMembership, StaffClassAccess


def get_class_ids_for_staff_in_school(
    db: Session,
    staff_id: uuid.UUID,
    school_id: uuid.UUID,
    scopes: Sequence[StaffClassScope] | None = None,
) -> list[uuid.UUID]:
    """Class ids a staff member holds access to in their school, optionally restricted to given
    access scopes (support is limited to scope=shared; teacher spans owner+shared)."""
    query = (
        select(StaffClassAccess.class_id)
        .join(ClassGroup, ClassGroup.id == StaffClassAccess.class_id)
        .where(StaffClassAccess.staff_id == staff_id, ClassGroup.school_id == school_id)
    )
    if scopes is not None:
        query = query.where(StaffClassAccess.scope.in_(list(scopes)))
    return list(db.scalars(query))


def student_in_any_class(db: Session, student_id: uuid.UUID, class_ids: list[uuid.UUID]) -> bool:
    if not class_ids:
        return False
    member = db.scalar(
        select(ClassMembership).where(
            ClassMembership.student_id == student_id, ClassMembership.class_id.in_(class_ids)
        )
    )
    return member is not None


def get_class(db: Session, class_id: uuid.UUID) -> ClassGroup | None:
    return db.get(ClassGroup, class_id)


def get_class_by_join_code(db: Session, code: str) -> ClassGroup | None:
    """A class join code resolves only within an ACTIVE school (FR-01-02 sign-in path)."""
    return db.scalar(
        select(ClassGroup)
        .join(School, School.id == ClassGroup.school_id)
        .where(ClassGroup.join_code == code, School.status == SchoolStatus.active)
    )


def get_class_by_qr_token(db: Session, token: str) -> ClassGroup | None:
    """The persistent per-class qr_token resolves only within an ACTIVE school (FR-01-02)."""
    return db.scalar(
        select(ClassGroup)
        .join(School, School.id == ClassGroup.school_id)
        .where(ClassGroup.qr_token == token, School.status == SchoolStatus.active)
    )


def student_in_class(db: Session, student_id: uuid.UUID, class_id: uuid.UUID) -> bool:
    return student_in_any_class(db, student_id, [class_id])


def set_class_access(
    db: Session, klass: ClassGroup, join_code: str, qr_token: str
) -> ClassGroup:
    """Persist a class's issued/rotated persistent join code + qr token (FR-01-02 issuance)."""
    klass.join_code = join_code
    klass.qr_token = qr_token
    db.flush()
    return klass
