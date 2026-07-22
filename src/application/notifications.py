"""Notification transport (INFRA-05) — email + in-app, async DB-outbox + in-process worker.

A producer enqueues a message; the transport owns delivery. In-app is stored immediately (feed
redundancy); email is queued and delivered by the worker with retry/backoff. Status moves
queued -> sent -> delivered | failed | retrying. A send that exhausts retries is escalated to ops
(CRITICAL) and stays visible in-app — never a silent loss. Recipients are in-school only.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain.enums import AlertChannel, DeliveryStatus
from src.infrastructure.email import send_email
from src.infrastructure.models.billing import Notification
from src.infrastructure.models.identity import StaffAccount

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
        row = Notification(
            recipient_id=recipient_id,
            type=ntype,
            payload=payload,
            channel=ch,
            delivery_status=status,
        )
        db.add(row)
        rows.append(row)
    db.flush()
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
    """Worker step: attempt all queued/retrying email notifications. Returns the count processed."""
    pending = list(
        db.scalars(
            select(Notification)
            .where(
                Notification.channel == AlertChannel.email,
                Notification.delivery_status.in_([DeliveryStatus.queued, DeliveryStatus.retrying]),
            )
            .with_for_update(skip_locked=True)  # claim rows -> no concurrent double-send
        )
    )
    for n in pending:
        _attempt_email(db, n)
    db.flush()
    return len(pending)


def _attempt_email(db: Session, n: Notification) -> None:
    recipient = db.get(StaffAccount, n.recipient_id)
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
    logger.critical(  # escalated to ops; the in-app copy remains visible (EC-016 redundancy)
        "fr_18_03_delivery_failed recipient=%s type=%s attempts=%s reason=%s",
        n.recipient_id,
        n.type,
        n.attempts,
        reason,
    )


def confirm_delivery(
    db: Session, notification_id: uuid.UUID, delivered: bool
) -> Notification | None:
    """Idempotent provider/worker callback."""
    n = db.get(Notification, notification_id)
    if n is None:
        return None
    if n.delivery_status in (DeliveryStatus.delivered, DeliveryStatus.failed):
        return n  # terminal states are idempotent; a late/out-of-order webhook can't flip them
    n.delivery_status = DeliveryStatus.delivered if delivered else DeliveryStatus.failed
    db.flush()
    return n


def feed_for(db: Session, recipient_id: uuid.UUID) -> list[Notification]:
    """In-app feed for a recipient (inherently school-scoped — staff belong to one school)."""
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
