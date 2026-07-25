"""Check-in + activities models (§13.1)."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from config.db_connection import Base
from src.constants.enums import (
    ActivityAgeBand,
    ActivityEngagementStatus,
    ActivityScope,
    ActivityType,
)


class CheckIn(Base):
    """FR-04-01 review remediation (Blocker 1, `docs/reviews/FR-04-01.md` Finding 1): `local_date`
    is the school-LOCAL calendar day the check-in was submitted on (computed by the application
    from `CalendarConfig.timezone` — the same day-boundary math `submit_checkin` already uses for
    its read-then-write duplicate check), persisted so the DB can enforce "one check-in per student
    per school-local day" itself via `uq_checkins_student_local_date`, instead of relying only on
    the app-level select-then-insert that the review proved loses a genuine concurrent race (8/10
    trials, two rows for one student/day). `submitted_at` alone cannot back this constraint: it is
    a UTC timestamptz and the local calendar day depends on a per-school timezone Postgres cannot
    resolve inside a single-table expression index."""

    __tablename__ = "check_ins"
    __table_args__ = (
        Index("ix_checkins_school_submitted", "school_id", "submitted_at"),
        UniqueConstraint(
            "student_id", "local_date", name="uq_checkins_student_local_date"
        ),
    )
    # `client_entry_id` (FR-04-06): the offline sync idempotency key. Column + unique index are
    # added by migration `d1e9a4b73f02`.

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    mood_value: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0..5 (0=most negative)
    reflection_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    captured_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    within_window: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    scored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)  # scoring queue
    score_attempts: Mapped[int] = mapped_column(  # bounded-retry counter before dead-letter
        SmallInteger, nullable=False, default=0
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    client_entry_id: Mapped[str | None] = mapped_column(String, nullable=True)


class Activity(Base):
    __tablename__ = "activities"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scope: Mapped[ActivityScope] = mapped_column(
        Enum(ActivityScope, name="activity_scope"), nullable=False, default=ActivityScope.seed
    )
    school_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id"), nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[ActivityType] = mapped_column(
        Enum(ActivityType, name="activity_type"), nullable=False
    )
    age_band: Mapped[ActivityAgeBand] = mapped_column(
        Enum(ActivityAgeBand, name="activity_age_band"), nullable=False, default=ActivityAgeBand.all
    )
    topic: Mapped[str | None] = mapped_column(String, nullable=True)


class ActivityEngagement(Base):
    __tablename__ = "activity_engagements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    activity_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("activities.id"), nullable=False)
    checkin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("check_ins.id"), nullable=True)
    status: Mapped[ActivityEngagementStatus] = mapped_column(
        Enum(ActivityEngagementStatus, name="activity_engagement_status"),
        nullable=False,
        default=ActivityEngagementStatus.offered,
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CheckInSettings(Base):
    """FR-04-01 (ticket Scenario 3): the school-level "require a reflection" toggle. NOT part of the
    FR-16-02 settings hub — that hub's three surfaces (concern words / alert routing / access
    window) never covered this, and FR-16-02's own ticket text does not mention it; this ticket adds
    the minimal row itself rather than reusing an unrelated table. Default `False` (reflection
    optional) so a school with no row yet behaves exactly like "optional" — never accidentally
    blocking every check-in the moment this ships.

    No leadership-facing write surface is built by this ticket (out of scope here) — see
    ``docs/DEFERRALS.md``. Tests and any future caller write it directly via
    ``src.domain.checkin.services.set_checkin_settings``.
    """

    __tablename__ = "checkin_settings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, unique=True, index=True
    )
    require_reflection: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
