"""Notification + delivery domain services — DB access only (backend.md: domain owns DB).

The Notification is the message; per-channel delivery state lives on AlertDelivery (the canonical
delivery table). Delivery DB helpers live here because the notification transport owns them.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import AlertChannel, DeliveryStatus, SubscriptionState, SubscriptionTier
from src.domain.billing.models import Notification, Subscription
from src.domain.risk.models import AlertDelivery


def add_notification(
    db: Session,
    *,
    recipient_id: uuid.UUID,
    ntype: str,
    payload: dict[str, Any] | None,
) -> Notification:
    row = Notification(recipient_id=recipient_id, type=ntype, payload=payload)
    db.add(row)
    db.flush()
    return row


def add_delivery(
    db: Session,
    *,
    notification_id: uuid.UUID,
    recipient_id: uuid.UUID,
    channel: AlertChannel,
    status: DeliveryStatus,
    flag_id: uuid.UUID | None = None,
    next_attempt_at: datetime | None = None,
    delivered_at: datetime | None = None,
) -> AlertDelivery:
    row = AlertDelivery(
        notification_id=notification_id,
        recipient_id=recipient_id,
        channel=channel,
        status=status,
        flag_id=flag_id,
        next_attempt_at=next_attempt_at,
        delivered_at=delivered_at,
    )
    db.add(row)
    db.flush()
    return row


def get_notification(db: Session, notification_id: uuid.UUID) -> Notification | None:
    return db.get(Notification, notification_id)


def get_delivery(db: Session, delivery_id: uuid.UUID) -> AlertDelivery | None:
    return db.get(AlertDelivery, delivery_id)


def get_email_delivery_for_notification(
    db: Session, notification_id: uuid.UUID
) -> AlertDelivery | None:
    """The provider webhook confirms the EMAIL delivery of a notification."""
    return db.scalar(
        select(AlertDelivery).where(
            AlertDelivery.notification_id == notification_id,
            AlertDelivery.channel == AlertChannel.email,
        )
    )


def get_due_deliveries(db: Session, now: datetime) -> list[AlertDelivery]:
    """Email deliveries the worker may attempt now: (queued|retrying) AND next_attempt_at <= now.

    SKIP LOCKED so concurrent worker passes never contend on the same row.
    """
    return list(
        db.scalars(
            select(AlertDelivery)
            .where(
                AlertDelivery.channel == AlertChannel.email,
                AlertDelivery.status.in_([DeliveryStatus.queued, DeliveryStatus.retrying]),
                AlertDelivery.next_attempt_at.is_not(None),
                AlertDelivery.next_attempt_at <= now,
            )
            .with_for_update(skip_locked=True)
        )
    )


def feed_notifications(db: Session, recipient_id: uuid.UUID) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(Notification.recipient_id == recipient_id)
            .order_by(Notification.created_at.desc())
        )
    )


def deliveries_for_recipient(db: Session, recipient_id: uuid.UUID) -> list[AlertDelivery]:
    return list(
        db.scalars(
            select(AlertDelivery).where(AlertDelivery.recipient_id == recipient_id)
        )
    )


def persist(db: Session) -> None:
    db.flush()


def arm_trial_subscription(db: Session, school_id: uuid.UUID) -> Subscription:
    """FR-02-02: on District/Trust approval, ARM the whole-school Premium trial — create the
    ``Subscription`` row with ``tier=premium``, ``state=trial``, but ``trial_start_at`` left NULL.
    The clock does NOT start here: FR-17-03 sets ``trial_start_at``/``trial_end_at`` from the
    school's first student check-in, never from approval. Called once per school, from the same
    ACID transaction that flips ``School.status`` to ``active`` — the 409 "not pending" guard in
    ``application/schools/services.py`` is what keeps this a create-once operation (a school can
    only be approved while pending, and approval flips it out of pending immediately)."""
    sub = Subscription(
        school_id=school_id, tier=SubscriptionTier.premium, state=SubscriptionState.trial
    )
    db.add(sub)
    db.flush()
    return sub


def get_subscription_by_school(db: Session, school_id: uuid.UUID) -> Subscription | None:
    return db.scalar(select(Subscription).where(Subscription.school_id == school_id))
