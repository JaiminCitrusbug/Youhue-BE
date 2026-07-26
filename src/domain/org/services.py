"""Org domain services — class-access / membership queries only."""
import uuid
from collections.abc import Sequence
from datetime import date, datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import InvitationStatus, SchoolStatus, StaffClassScope
from src.domain.identity.models import School
from src.domain.org.models import (
    CalendarConfig,
    ClassGroup,
    ClassMembership,
    Invitation,
    StaffClassAccess,
)


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


def get_student_ids_in_class(db: Session, class_id: uuid.UUID) -> list[uuid.UUID]:
    """FR-10-01 — the class roster's student ids, for the dashboard's mood-index aggregate."""
    return list(
        db.scalars(
            select(ClassMembership.student_id).where(ClassMembership.class_id == class_id)
        )
    )


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


def get_classes_for_staff(
    db: Session, staff_id: uuid.UUID, school_id: uuid.UUID, scopes: Sequence[StaffClassScope]
) -> list[ClassGroup]:
    """Every class the staff member holds ANY of the given access scopes on, in their school —
    name order for a stable picker (FR-02-03's 'Shared class' select needs the owner's own
    classes; no prior ticket exposed a class-list read)."""
    return list(
        db.scalars(
            select(ClassGroup)
            .join(StaffClassAccess, StaffClassAccess.class_id == ClassGroup.id)
            .where(
                StaffClassAccess.staff_id == staff_id,
                ClassGroup.school_id == school_id,
                StaffClassAccess.scope.in_(list(scopes)),
            )
            .order_by(ClassGroup.name.asc())
        )
    )


def get_staff_class_access(
    db: Session, staff_id: uuid.UUID, class_id: uuid.UUID
) -> StaffClassAccess | None:
    return db.scalar(
        select(StaffClassAccess).where(
            StaffClassAccess.staff_id == staff_id, StaffClassAccess.class_id == class_id
        )
    )


def grant_class_access(
    db: Session, staff_id: uuid.UUID, class_id: uuid.UUID, scope: StaffClassScope
) -> StaffClassAccess:
    """Idempotent: a staff member who already holds ANY access row for this class keeps it
    unchanged (never downgrades an existing owner to shared) rather than erroring."""
    access = get_staff_class_access(db, staff_id, class_id)
    if access is not None:
        return access
    access = StaffClassAccess(staff_id=staff_id, class_id=class_id, scope=scope)
    db.add(access)
    db.flush()
    return access


def create_invitation(
    db: Session,
    *,
    school_id: uuid.UUID,
    class_id: uuid.UUID,
    inviter_id: uuid.UUID,
    email: str,
    token: str,
    expires_at: datetime,
) -> Invitation:
    invitation = Invitation(
        school_id=school_id,
        class_id=class_id,
        inviter_id=inviter_id,
        email=email.lower(),
        token=token,
        expires_at=expires_at,
    )
    db.add(invitation)
    db.flush()
    return invitation


def get_invitation(db: Session, invitation_id: uuid.UUID) -> Invitation | None:
    return db.get(Invitation, invitation_id)


def get_invitation_by_token(db: Session, token: str) -> Invitation | None:
    return db.scalar(select(Invitation).where(Invitation.token == token))


def list_invitations_for_class(db: Session, class_id: uuid.UUID) -> list[Invitation]:
    """Every invitation ever sent for this class (any status) — the 'Pending invitations' table
    (SC-059) needs real rows to manage, not a fixture."""
    return list(
        db.scalars(
            select(Invitation).where(Invitation.class_id == class_id).order_by(Invitation.email)
        )
    )


def get_pending_invitation_for_class(
    db: Session, class_id: uuid.UUID, email: str
) -> Invitation | None:
    """An outstanding (not yet accepted/revoked/expired) invitation to this email for this class,
    if any — the duplicate-invite guard (ticket 409 'already invited')."""
    return db.scalar(
        select(Invitation).where(
            Invitation.class_id == class_id,
            Invitation.email == email.lower(),
            Invitation.status.in_((InvitationStatus.invited, InvitationStatus.sent)),
        )
    )


def get_calendar_config(db: Session, school_id: uuid.UUID) -> CalendarConfig | None:
    """A school's access-window config (FR-16-02 SC-063), if one has been saved yet."""
    return db.scalar(select(CalendarConfig).where(CalendarConfig.school_id == school_id))


def set_calendar_config(
    db: Session,
    school_id: uuid.UUID,
    window_start: time,
    window_end: time,
    timezone: str,
    term_start: date | None = None,
    term_end: date | None = None,
) -> CalendarConfig:
    """Upsert a school's access-window + term-dates config (FR-16-02 window/tz; FR-07-04 added the
    minimal term_start/term_end pair to this SAME row). ``term_start``/``term_end`` are OPTIONAL —
    when the caller omits them (not part of this save), any previously-saved term dates are left
    untouched, same as ``holidays`` today (no leadership surface to clear either yet). Caller
    commits."""
    row = get_calendar_config(db, school_id)
    if row is None:
        row = CalendarConfig(
            school_id=school_id,
            window_start=window_start,
            window_end=window_end,
            timezone=timezone,
            term_start=term_start,
            term_end=term_end,
        )
        db.add(row)
    else:
        row.window_start = window_start
        row.window_end = window_end
        row.timezone = timezone
        if term_start is not None and term_end is not None:
            row.term_start = term_start
            row.term_end = term_end
    db.flush()
    return row
