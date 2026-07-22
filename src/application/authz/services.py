"""Role & permission authorization (INFRA-03) — SRS §8. Business logic; DB via domain services.

Fine-grained, role- AND school- AND class-scoped, decided server-side and tested as the disallowed
actor (403). District sees aggregates only — never student-level rows.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.constants.enums import StaffRole
from src.domain.identity.models import StaffAccount, Student
from src.domain.org import services as org_db

_WHOLE_SCHOOL_ROLES = (StaffRole.leadership,)
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
        class_ids = org_db.get_class_ids_for_staff_in_school(db, staff.id, staff.school_id)
        return org_db.student_in_any_class(db, student.id, class_ids)
    return False


def require_student_access(db: Session, staff: StaffAccount, student: Student) -> None:
    if not can_access_student(db, staff, student):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
