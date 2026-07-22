"""Notification transport (INFRA-05) business logic — email + in-app, async outbox + worker.

Retry/backoff, surfaced-not-lost, idempotent delivery, in-school recipients. DB via domain services.
"""
import logging
import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.constants.enums import AlertChannel, DeliveryStatus
from src.domain.billing import services as billing_db
from src.domain.billing.models import Notification
from src.domain.identity import services as identity_db
from src.infrastructure.emailer import send_email

logger = logging.getLogger("youhue.notifications")

DEFAULT_CHANNELS = (AlertChannel.in_app, AlertChannel.email)
MAX_ATTEMPTS = 3


def enqueue(
    db: Session,
    *,
    recipient_id: uuid.UUID,
    ntype: str,
    payload: dict[str, Any] | None,
    channels: tuple[AlertChannel, ...] = DEFAULT_CHANNELS,
) -> list[Notification]:
    rows: list[Notification] = []
    for ch in channels:
        status = DeliveryStatus.delivered if ch == AlertChannel.in_app else DeliveryStatus.queued
        rows.append(
            billing_db.add_notification(
                db, recipient_id=recipient_id, ntype=ntype, payload=payload,
                channel=ch, delivery_status=status,
            )
        )
    return rows


def _email_content(n: Notification) -> tuple[str, str]:
    # channel-appropriate: the actionable reason, NOT the raw payload/PII (detail stays in-app)
    reason = ""
    if isinstance(n.payload, dict):
        value = n.payload.get("reason")
        if isinstance(value, str):
            reason = f" — {value}"
    body = f"You have a Youhue notification: {n.type}{reason}. Sign in to view."
    return f"[Youhue] {n.type}", body


def dispatch_pending(db: Session) -> int:
    pending = billing_db.get_pending_email_for_update(db)
    for n in pending:
        _attempt_email(db, n)
    billing_db.persist(db)
    return len(pending)


def _attempt_email(db: Session, n: Notification) -> None:
    recipient = identity_db.get_staff(db, n.recipient_id)
    if recipient is None:
        _fail(n, reason="unknown recipient")
        return
    n.attempts += 1
    try:
        subject, body = _email_content(n)
        send_email(recipient.email, subject, body)
        n.delivery_status = DeliveryStatus.sent  # 'delivered' confirmed by the provider webhook
    except Exception:  # noqa: BLE001 - any send error is retried with backoff, never dropped
        if n.attempts >= MAX_ATTEMPTS:
            _fail(n, reason="max attempts exhausted")
        else:
            n.delivery_status = DeliveryStatus.retrying


def _fail(n: Notification, *, reason: str) -> None:
    n.delivery_status = DeliveryStatus.failed
    logger.critical(
        "fr_18_03_delivery_failed recipient=%s type=%s attempts=%s reason=%s",
        n.recipient_id, n.type, n.attempts, reason,
    )


def confirm_delivery(
    db: Session, notification_id: uuid.UUID, delivered: bool
) -> Notification | None:
    n = billing_db.get_notification(db, notification_id)
    if n is None:
        return None
    if n.delivery_status in (DeliveryStatus.delivered, DeliveryStatus.failed):
        return n  # terminal states are idempotent; a late webhook can't flip them
    n.delivery_status = DeliveryStatus.delivered if delivered else DeliveryStatus.failed
    billing_db.persist(db)
    return n


def feed_for(db: Session, recipient_id: uuid.UUID) -> list[Notification]:
    return billing_db.in_app_feed(db, recipient_id)


def enqueue_to_school(
    db: Session,
    sender_school_id: uuid.UUID | None,
    recipient_id: uuid.UUID,
    ntype: str,
    payload: dict[str, Any] | None,
) -> int:
    """Enqueue to an IN-SCHOOL recipient only (403 otherwise) and attempt delivery now."""
    recipient = identity_db.get_staff(db, recipient_id)
    if recipient is None or recipient.school_id != sender_school_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Recipient not in your school")
    rows = enqueue(db, recipient_id=recipient_id, ntype=ntype, payload=payload)
    dispatch_pending(db)
    return len(rows)
