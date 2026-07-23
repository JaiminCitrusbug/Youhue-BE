"""Identity domain services — DB queries only (backend.md: domain owns DB access)."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import SchoolStatus, StaffRole, StaffStatus, StudentStatus
from src.domain.identity.models import InternalAdmin, School, StaffAccount, Student


def get_admin_by_email(db: Session, email: str) -> InternalAdmin | None:
    """Look up a platform-level internal admin by email (admin console sign-in, FR-19-01)."""
    return db.scalar(select(InternalAdmin).where(InternalAdmin.email == email.lower()))


def get_admin(db: Session, admin_id: uuid.UUID) -> InternalAdmin | None:
    return db.get(InternalAdmin, admin_id)


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


def get_school(db: Session, school_id: uuid.UUID) -> School | None:
    return db.get(School, school_id)


def get_active_school_by_name(db: Session, name: str) -> School | None:
    """An APPROVED (active) school matching this name, case-insensitively (FR-02-01 Scenario 3 —
    a later teacher from the same school joins it rather than creating a duplicate)."""
    return db.scalar(
        select(School).where(
            func.lower(School.name) == name.strip().lower(),
            School.status == SchoolStatus.active,
        )
    )


def sign_in_code_exists(db: Session, code: str) -> bool:
    """A school sign-in code is globally unique; used to avoid a collision on generation."""
    return db.scalar(select(School.id).where(School.sign_in_code == code)) is not None


def create_school(db: Session, name: str, sign_in_code: str) -> School:
    """Create a school in the default PENDING state (SchoolStatus.pending / SchoolTier.free are the
    model defaults). It is NOT live until a District/Trust admin approves it (FR-02-02)."""
    school = School(name=name, sign_in_code=sign_in_code)
    db.add(school)
    db.flush()
    return school


def create_staff(
    db: Session,
    school_id: uuid.UUID,
    email: str,
    password_hash: str,
    role: StaffRole = StaffRole.teacher,
    status: StaffStatus = StaffStatus.active,
) -> StaffAccount:
    """Associate a staff account with a school (FR-02-01 makes the registering teacher an active
    owner so they can sign in to see the pending-approval state)."""
    staff = StaffAccount(
        school_id=school_id,
        email=email.lower(),
        password_hash=password_hash,
        role=role,
        status=status,
    )
    db.add(staff)
    db.flush()
    return staff


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
