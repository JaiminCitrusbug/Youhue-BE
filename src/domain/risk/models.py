"""Risk / alerting models (§13.1). M-12 is the sole writer of Flag / risk_score / band."""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from config.db_connection import Base
from src.constants.enums import (
    AlertChannel,
    DeliveryStatus,
    FlagBand,
    FlagEventType,
    FlagStatus,
    FlagType,
)


class ConcernWordList(Base):
    __tablename__ = "concern_word_lists"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("schools.id"), nullable=True)
    words: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (
        Index("ix_flags_school_status", "school_id", "status"),
        # one flag per scored check-in -> scoring is idempotent on retry (INFRA-06 §Must-nots).
        UniqueConstraint("checkin_id", name="uq_flags_checkin_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    checkin_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("check_ins.id"), nullable=True)
    type: Mapped[FlagType] = mapped_column(Enum(FlagType, name="flag_type"), nullable=False)
    risk_score: Mapped[float] = mapped_column(Numeric(3, 2), nullable=False)
    # band is the ACTION band — decided by FR-12-06 routing, not this pipeline; null until routed.
    band: Mapped[FlagBand | None] = mapped_column(Enum(FlagBand, name="flag_band"), nullable=True)
    status: Mapped[FlagStatus] = mapped_column(
        Enum(FlagStatus, name="flag_status"), nullable=False, default=FlagStatus.open
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlertRecipientConfig(Base):
    __tablename__ = "alert_recipient_configs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    alert_type: Mapped[str] = mapped_column(String, nullable=False)
    recipient_staff_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(Uuid), nullable=False)
    order_index: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)


class AlertDelivery(Base):
    """Per-channel delivery lifecycle (INFRA-05). One row per (notification, channel).

    Owns delivery state/attempts/backoff so the Notification stays the message only. `flag_id` is
    nullable because invites/transactional notifications have no flag.
    """

    __tablename__ = "alert_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    notification_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notifications.id"), nullable=False, index=True
    )
    flag_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("flags.id"), nullable=True, index=True
    )
    recipient_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_accounts.id"), nullable=False)
    channel: Mapped[AlertChannel] = mapped_column(
        Enum(AlertChannel, name="alert_channel"), nullable=False
    )
    status: Mapped[DeliveryStatus] = mapped_column(
        Enum(DeliveryStatus, name="delivery_status"), nullable=False, default=DeliveryStatus.queued
    )
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    # backoff: the earliest time the worker may (re)attempt this delivery; NULL once terminal/sent.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FlagEvent(Base):
    """Append-only / immutable — no UPDATE/DELETE at the app layer."""

    __tablename__ = "flag_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    flag_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("flags.id"), nullable=False, index=True)
    event_type: Mapped[FlagEventType] = mapped_column(
        Enum(FlagEventType, name="flag_event_type"), nullable=False
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("staff_accounts.id"), nullable=True
    )
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SupportiveNote(Base):
    __tablename__ = "supportive_notes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_accounts.id"), nullable=False)
    flag_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("flags.id"), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_private: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
