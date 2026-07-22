"""Check-in + activities models (§13.1)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.domain.enums import ActivityAgeBand, ActivityEngagementStatus, ActivityScope, ActivityType
from src.infrastructure.db import Base


class CheckIn(Base):
    __tablename__ = "check_ins"
    __table_args__ = (Index("ix_checkins_school_submitted", "school_id", "submitted_at"),)

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
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


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
