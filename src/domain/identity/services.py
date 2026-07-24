"""Identity domain services — DB queries only (backend.md: domain owns DB access)."""
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import SchoolStatus, StaffRole, StaffStatus, StudentAgeBand, StudentStatus
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


def list_staff_in_school(db: Session, school_id: uuid.UUID) -> list[StaffAccount]:
    """Every staff account at a school, any status (FR-16-02 SC-057 staff-management list) —
    oldest-first so the leadership view is stable across reloads."""
    return list(
        db.scalars(
            select(StaffAccount)
            .where(StaffAccount.school_id == school_id)
            .order_by(StaffAccount.created_at.asc())
        )
    )


def get_staff_in_school(
    db: Session, school_id: uuid.UUID, staff_id: uuid.UUID
) -> StaffAccount | None:
    """A staff account, scoped to one school (FR-16-02 same-school-only) — never returns a row
    belonging to a different tenant, whatever ``staff_id`` is passed."""
    return db.scalar(
        select(StaffAccount).where(StaffAccount.id == staff_id, StaffAccount.school_id == school_id)
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


def list_schools(db: Session, name_contains: str | None = None) -> list[School]:
    """Every school on the platform (FR-19-02 / SC-075), optionally name-filtered. Admin-console
    only — callers are RBAC-gated by the application layer before reaching this."""
    stmt = select(School).order_by(School.name)
    if name_contains:
        stmt = stmt.where(func.lower(School.name).contains(name_contains.strip().lower()))
    return list(db.scalars(stmt))


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


def get_student_by_external_ref(
    db: Session, school_id: uuid.UUID, external_ref: str
) -> Student | None:
    """FR-03-01 roster import: the upsert/idempotency key for a re-uploaded row that carries the
    school's own student identifier. Backed by the ``uq_students_school_external_ref`` partial
    unique index (migration a1b2c3d4e5f6) — this select is the fast path, the index is the race
    backstop for a genuinely concurrent retry (mirrors ``uq_schools_name_live``, FR-02-01)."""
    return db.scalar(
        select(Student).where(
            Student.school_id == school_id, Student.external_ref == external_ref
        )
    )


def list_students_in_school(db: Session, school_id: uuid.UUID) -> list[Student]:
    """FR-03-05 SC-037 roster list view: every student at a school, any status (active AND
    inactive/deactivated leavers are both shown — the screen must be able to display a leaver's
    'Inactive' tag, never hide the row). Name order for a stable, scannable staff-facing list."""
    return list(
        db.scalars(
            select(Student)
            .where(Student.school_id == school_id)
            .order_by(Student.display_name.asc())
        )
    )


def list_active_students_with_external_ref_outside(
    db: Session, school_id: uuid.UUID, keep_refs: set[str]
) -> list[Student]:
    """FR-03-05 reconciliation: every currently-ACTIVE student at this school who carries an
    ``external_ref`` NOT present in ``keep_refs`` (the set of external_refs seen in the freshly
    uploaded roster) — these are the leavers to deactivate. Deliberately scoped to
    ``external_ref IS NOT NULL``: a student with no natural key can never be reliably matched
    across two uploads (the same documented residual as ``get_student_by_external_ref``'s
    always-CREATE no-ref row), so a no-ref row is left untouched by reconciliation rather than
    being deactivated on every re-import purely because it can't be matched."""
    if not keep_refs:
        return list(
            db.scalars(
                select(Student).where(
                    Student.school_id == school_id,
                    Student.status == StudentStatus.active,
                    Student.external_ref.is_not(None),
                )
            )
        )
    return list(
        db.scalars(
            select(Student).where(
                Student.school_id == school_id,
                Student.status == StudentStatus.active,
                Student.external_ref.is_not(None),
                Student.external_ref.not_in(keep_refs),
            )
        )
    )


def create_student(
    db: Session,
    *,
    school_id: uuid.UUID,
    display_name: str,
    age_band: StudentAgeBand,
    status: StudentStatus = StudentStatus.active,
    external_ref: str | None = None,
) -> Student:
    """FR-03-01 roster import: create one student, already age-banded (GATE G-2 is satisfied by
    construction — ``age_band`` is NOT NULL on the model, so no student can exist unbanded)."""
    student = Student(
        school_id=school_id,
        display_name=display_name,
        age_band=age_band,
        status=status,
        external_ref=external_ref,
    )
    db.add(student)
    db.flush()
    return student


def link_sso_subject(db: Session, staff: StaffAccount, subject: str) -> None:
    staff.sso_subject = subject
    db.flush()


def set_password_hash(db: Session, staff: StaffAccount, password_hash: str) -> None:
    staff.password_hash = password_hash
    db.flush()
