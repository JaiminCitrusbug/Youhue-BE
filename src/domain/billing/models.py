"""Subscription + notification models (§13.1). No card/payment fields (external billing)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Enum, ForeignKey, SmallInteger, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from config.db_connection import Base
from src.constants.enums import SubscriptionState, SubscriptionTier


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        # FR-19-02 review fix: unique (not just indexed) — at most one Subscription row per
        # school, ever. DB-level backstop for `get_or_create_subscription`'s upsert (see
        # `src/domain/billing/services.py`); enforced by migration f4a9c1e7b382.
        ForeignKey("schools.id"), nullable=False, unique=True, index=True
    )
    tier: Mapped[SubscriptionTier] = mapped_column(
        Enum(SubscriptionTier, name="subscription_tier"),
        nullable=False,
        default=SubscriptionTier.free,
    )
    state: Mapped[SubscriptionState] = mapped_column(
        Enum(SubscriptionState, name="subscription_state"),
        nullable=False,
        default=SubscriptionState.trial,
    )
    trial_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_extension_count: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0)
    term_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Notification(Base):
    """The MESSAGE only (INFRA-05). Per-channel delivery state lives on AlertDelivery."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    recipient_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("staff_accounts.id"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # FR-18-01 (SC-054 notifications centre): null = unread; set by mark-all-read. Per-notification
    # (not per-channel) — the in-app "read" concept is distinct from AlertDelivery's send lifecycle.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
