"""Role & permission authorization (INFRA-03) — SRS §8.

Fine-grained, role- AND school- AND class-scoped, decided server-side and tested as the disallowed
actor (403). Support = teacher toolset scoped to a shared class. Leadership/district see their whole
school; student-level data never leaves its school. MFA is required for leadership/district/admin.
Cross-school / district comparison surfaces are Phase-3 and are simply absent in Phase 1.
"""
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.domain.enums import StaffRole
from src.infrastructure.models.identity import StaffAccount, Student
from src.infrastructure.models.org import ClassGroup, ClassMembership, StaffClassAccess

# Roles that may read every student in their OWN school (student-level access).
# District is intentionally EXCLUDED: district has only aggregate surfaces — student-level data
# never leaves its school (SRS §8). So district falls through to False in can_access_student.
_WHOLE_SCHOOL_ROLES = (StaffRole.leadership,)
# Roles scoped to the classes they own/share.
_CLASS_SCOPED_ROLES = (StaffRole.teacher, StaffRole.support)


def role_requires_mfa(role: StaffRole) -> bool:
    return role.value in settings.mfa_roles


def require_roles(staff: StaffAccount, *allowed: StaffRole) -> None:
    if staff.role not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")


def can_access_student(db: Session, staff: StaffAccount, student: Student) -> bool:
    if staff.school_id != student.school_id:
        return False  # cross-school: never
    if staff.role in _WHOLE_SCHOOL_ROLES:
        return True
    if staff.role in _CLASS_SCOPED_ROLES:
        # class must belong to the staff's own school (defense-in-depth on top of the school check)
        class_ids = list(
            db.scalars(
                select(StaffClassAccess.class_id)
                .join(ClassGroup, ClassGroup.id == StaffClassAccess.class_id)
                .where(
                    StaffClassAccess.staff_id == staff.id,
                    ClassGroup.school_id == staff.school_id,
                )
            )
        )
        if not class_ids:
            return False
        member = db.scalar(
            select(ClassMembership).where(
                ClassMembership.student_id == student.id,
                ClassMembership.class_id.in_(class_ids),
            )
        )
        return member is not None
    return False


def require_student_access(db: Session, staff: StaffAccount, student: Student) -> None:
    if not can_access_student(db, staff, student):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
