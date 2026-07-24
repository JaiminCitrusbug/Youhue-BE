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


def get_staff_by_email_in_school(
    db: Session, email: str, school_id: uuid.UUID
) -> StaffAccount | None:
    """Any status. The registrant of a still-pending school is deliberately NOT active yet
    (FR-02-01), so the registration flow cannot use the active-only lookup below."""
    return db.scalar(
        select(StaffAccount).where(
            StaffAccount.email == email.lower(), StaffAccount.school_id == school_id
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


def get_school_for_decision(db: Session, school_id: uuid.UUID) -> School | None:
    """FR-02-02: lock the row for the duration of the decision transaction. Two concurrent
    decisions on the same school (double-click, or a retried request) must not both win — the
    second sees the first's committed status, not a stale 'pending' read. ``SELECT ... FOR UPDATE``
    is the DB backstop the application read-then-write cannot be (mirrors the FR-02-01 registration
    race guard, applied here to the approve/reject transition instead of the create)."""
    return db.scalar(select(School).where(School.id == school_id).with_for_update())


def list_pending_schools(db: Session) -> list[School]:
    """FR-02-02 approval queue (SC-069) — oldest first, so a District reviewer works the backlog in
    submission order."""
    return list(
        db.scalars(
            select(School)
            .where(School.status == SchoolStatus.pending)
            .order_by(School.created_at.asc())
        )
    )


def get_registrant_of_school(db: Session, school_id: uuid.UUID) -> StaffAccount | None:
    """The staff account that registered this school (FR-02-01 creates exactly one, ``invited``,
    at registration time). Earliest-created wins if more than one ever exists at this school."""
    return db.scalar(
        select(StaffAccount)
        .where(StaffAccount.school_id == school_id)
        .order_by(StaffAccount.created_at.asc())
        .limit(1)
    )


def activate_invited_staff_in_school(db: Session, school_id: uuid.UUID) -> None:
    """FR-02-02: approval activates the school AND its registrant together (FR-02-01's registrant
    is deliberately left ``invited`` — see ``application/schools/services.py`` — because it becomes
    usable only here). Every still-``invited`` account at the school is activated, not only one row,
    so the guarantee holds even if more than one invited account exists."""
    staff_rows = db.scalars(
        select(StaffAccount).where(
            StaffAccount.school_id == school_id, StaffAccount.status == StaffStatus.invited
        )
    )
    for staff in staff_rows:
        staff.status = StaffStatus.active
    db.flush()


def count_students_in_school(db: Session, school_id: uuid.UUID) -> int:
    return db.scalar(
        select(func.count()).select_from(Student).where(Student.school_id == school_id)
    ) or 0


def get_school_by_name(db: Session, name: str) -> School | None:
    """A school that already holds this name, case-insensitively — PENDING or APPROVED alike
    (FR-02-01 Scenario 3: a later teacher must not create a duplicate). Mirrors the
    ``uq_schools_name_live`` partial unique index exactly, including its exclusion of REJECTED
    schools, whose name is released for a fresh registration.

    ``name`` is expected to arrive whitespace-normalised (see the application service)."""
    return db.scalar(
        select(School).where(
            func.lower(School.name) == name.strip().lower(),
            School.status != SchoolStatus.rejected,
        )
    )


def school_is_active(db: Session, school_id: uuid.UUID) -> bool:
    """Is this school LIVE? A self-registered school is pending until a District/Trust admin
    approves it (FR-02-01/FR-02-02) and, while pending, its staff get no session and reach no
    staff route — 'pending' must mean pending on every surface, not only the student one."""
    return (
        db.scalar(
            select(School.id).where(
                School.id == school_id, School.status == SchoolStatus.active
            )
        )
        is not None
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
    status: StaffStatus = StaffStatus.invited,
) -> StaffAccount:
    """Associate a staff account with a school.

    ``status`` defaults to the model's least-privilege ``invited`` — an account is only ever made
    ACTIVE by a deliberate, explicit decision. FR-02-01's self-registrant stays ``invited`` until
    the District/Trust approval (FR-02-02) activates the school and its registrant together."""
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
