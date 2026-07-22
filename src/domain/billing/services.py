"""Notification domain services — DB access only."""
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import AlertChannel, DeliveryStatus
from src.domain.billing.models import Notification


def add_notification(
    db: Session,
    *,
    recipient_id: uuid.UUID,
    ntype: str,
    payload: dict[str, Any] | None,
    channel: AlertChannel,
    delivery_status: DeliveryStatus,
) -> Notification:
    row = Notification(
        recipient_id=recipient_id,
        type=ntype,
        payload=payload,
        channel=channel,
        delivery_status=delivery_status,
    )
    db.add(row)
    db.flush()
    return row


def get_pending_email_for_update(db: Session) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(
                Notification.channel == AlertChannel.email,
                Notification.delivery_status.in_([DeliveryStatus.queued, DeliveryStatus.retrying]),
            )
            .with_for_update(skip_locked=True)
        )
    )


def get_notification(db: Session, notification_id: uuid.UUID) -> Notification | None:
    return db.get(Notification, notification_id)


def in_app_feed(db: Session, recipient_id: uuid.UUID) -> list[Notification]:
    return list(
        db.scalars(
            select(Notification)
            .where(
                Notification.recipient_id == recipient_id,
                Notification.channel == AlertChannel.in_app,
            )
            .order_by(Notification.created_at.desc())
        )
    )


def persist(db: Session) -> None:
    db.flush()
