"""Identity domain services — DB queries only (backend.md: domain owns DB access)."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import SchoolStatus, StaffStatus, StudentStatus
from src.domain.identity.models import School, StaffAccount, Student


def get_active_staff_by_email(db: Session, email: str) -> StaffAccount | None:
    return db.scalar(
        select(StaffAccount).where(
            StaffAccount.email == email.lower(), StaffAccount.status == StaffStatus.active
        )
    )


def get_staff(db: Session, staff_id: uuid.UUID) -> StaffAccount | None:
    return db.get(StaffAccount, staff_id)


def get_staff_by_sso_subject(
    db: Session, subject: str, school_id: uuid.UUID
) -> StaffAccount | None:
    return db.scalar(
        select(StaffAccount).where(
            StaffAccount.sso_subject == subject, StaffAccount.school_id == school_id
        )
    )


def get_active_staff_by_email_in_school(
    db: Session, email: str, school_id: uuid.UUID
) -> StaffAccount | None:
    return db.scalar(
        select(StaffAccount).where(
            StaffAccount.email == email.lower(),
            StaffAccount.school_id == school_id,
            StaffAccount.status == StaffStatus.active,
        )
    )


def get_active_school_by_code(db: Session, code: str) -> School | None:
    return db.scalar(
        select(School).where(School.sign_in_code == code, School.status == SchoolStatus.active)
    )


def get_active_student_in_school(
    db: Session, student_id: uuid.UUID, school_id: uuid.UUID
) -> Student | None:
    return db.scalar(
        select(Student).where(
            Student.id == student_id,
            Student.school_id == school_id,
            Student.status == StudentStatus.active,
        )
    )


def get_student(db: Session, student_id: uuid.UUID) -> Student | None:
    return db.get(Student, student_id)


def link_sso_subject(db: Session, staff: StaffAccount, subject: str) -> None:
    staff.sso_subject = subject
    db.flush()


def set_password_hash(db: Session, staff: StaffAccount, password_hash: str) -> None:
    staff.password_hash = password_hash
    db.flush()
