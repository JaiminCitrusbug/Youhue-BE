"""Org structure models (§13.1): classes, memberships, staff-class access, invitations, calendar."""

import uuid
from datetime import date, datetime, time

from sqlalchemy import Date, DateTime, Enum, ForeignKey, String, Time, UniqueConstraint, Uuid, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from config.db_connection import Base
from src.constants.enums import InvitationStatus, StaffClassScope


class ClassGroup(Base):
    __tablename__ = "class_groups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # FR-01-02: persistent per-class sign-in credentials issued/rotated by the class-access
    # service (teacher UI is FR-01-09). Rotate-only — no auto-expiry. Null until first issued.
    join_code: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    qr_token: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)


class ClassMembership(Base):
    __tablename__ = "class_memberships"
    __table_args__ = (UniqueConstraint("class_id", "student_id", name="uq_class_student"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    class_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("class_groups.id"), nullable=False, index=True
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )


class StaffClassAccess(Base):
    __tablename__ = "staff_class_access"
    __table_args__ = (UniqueConstraint("staff_id", "class_id", name="uq_staff_class"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    staff_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.id"), nullable=False, index=True
    )
    class_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("class_groups.id"), nullable=False, index=True
    )
    scope: Mapped[StaffClassScope] = mapped_column(
        Enum(StaffClassScope, name="staff_class_scope"),
        nullable=False,
        default=StaffClassScope.owner,
    )


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    class_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("class_groups.id"), nullable=True)
    inviter_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_accounts.id"), nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False)
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    status: Mapped[InvitationStatus] = mapped_column(
        Enum(InvitationStatus, name="invitation_status"),
        nullable=False,
        default=InvitationStatus.invited,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CalendarConfig(Base):
    __tablename__ = "calendar_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    window_start: Mapped[time] = mapped_column(Time, nullable=False)
    window_end: Mapped[time] = mapped_column(Time, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="UTC")
    holidays: Mapped[list[date] | None] = mapped_column(ARRAY(Date), nullable=True)
    # FR-07-04: minimal term-dates surface added to this SAME table (FR-16-02 covered only
    # window_start/window_end/timezone; term dates did not exist yet). Both null until leadership
    # saves a term via the SC-063 settings hub — 'this term' resolution 404s until then.
    term_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    term_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
