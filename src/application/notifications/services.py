"""Notification transport (INFRA-05) business logic — email + in-app, async outbox + worker.

Retry/backoff, surfaced-not-lost, idempotent delivery, in-school recipients. DB via domain services.

Delivery model: a Notification is the message; each channel gets an AlertDelivery row carrying its
own lifecycle (queued -> sent -> delivered | failed | retrying), attempts, and backoff clock. The
producer enqueue returns 202 immediately (in-app delivered inline for redundancy, email queued);
the email send happens off the request path via `process_due_deliveries` (the worker entrypoint).
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.constants.enums import AlertChannel, DeliveryStatus
from src.domain.billing import services as billing_db
from src.domain.billing.models import Notification
from src.domain.identity import services as identity_db
from src.domain.risk.models import AlertDelivery
from src.infrastructure.emailer import send_email

logger = logging.getLogger("youhue.notifications")

DEFAULT_CHANNELS = (AlertChannel.in_app, AlertChannel.email)
MAX_ATTEMPTS = 3
BASE_BACKOFF_SECONDS = 60  # exponential base: retry N waits 60 * 2**(N-1) seconds


def _now() -> datetime:
    return datetime.now(UTC)


def backoff_delay(attempts: int) -> timedelta:
    """Exponential backoff: attempt 1 -> 60s, 2 -> 120s, 3 -> 240s ... (grows with attempts)."""
    exponent = max(attempts - 1, 0)
    return timedelta(seconds=BASE_BACKOFF_SECONDS * (2**exponent))


def enqueue(
    db: Session,
    *,
    recipient_id: uuid.UUID,
    ntype: str,
    payload: dict[str, Any] | None,
    flag_id: uuid.UUID | None = None,
    channels: tuple[AlertChannel, ...] = DEFAULT_CHANNELS,
) -> Notification:
    """Create the message + one queued/delivered AlertDelivery per channel. No blocking send.

    in-app is delivered inline (the redundancy surface); email is queued with next_attempt_at=now
    so the worker picks it up on its next pass.
    """
    notification = billing_db.add_notification(
        db, recipient_id=recipient_id, ntype=ntype, payload=payload
    )
    now = _now()
    for ch in channels:
        if ch == AlertChannel.in_app:
            billing_db.add_delivery(
                db, notification_id=notification.id, recipient_id=recipient_id, channel=ch,
                status=DeliveryStatus.delivered, flag_id=flag_id, delivered_at=now,
            )
        else:  # email — queued for the worker
            billing_db.add_delivery(
                db, notification_id=notification.id, recipient_id=recipient_id, channel=ch,
                status=DeliveryStatus.queued, flag_id=flag_id, next_attempt_at=now,
            )
    return notification


def _email_content(n: Notification) -> tuple[str, str]:
    # channel-appropriate: the actionable reason, NOT the raw payload/PII (detail stays in-app)
    reason = ""
    if isinstance(n.payload, dict):
        value = n.payload.get("reason")
        if isinstance(value, str):
            reason = f" — {value}"
    body = f"You have a Youhue notification: {n.type}{reason}. Sign in to view."
    return f"[Youhue] {n.type}", body


def process_due_deliveries(db: Session) -> int:
    """Worker pass: attempt every email delivery that is due. Returns how many were processed.

    This is the single retry engine — invoked by the worker entrypoint (see worker.py), NOT inline
    on the producer request. One pass; the always-on scheduler is deferred (DEFERRALS.md).
    """
    due = billing_db.get_due_deliveries(db, _now())
    for delivery in due:
        _attempt_email(db, delivery)
    billing_db.persist(db)
    return len(due)


def _attempt_email(db: Session, delivery: AlertDelivery) -> None:
    recipient = identity_db.get_staff(db, delivery.recipient_id)
    if recipient is None:
        _fail(delivery, reason="unknown recipient")
        return
    notification = billing_db.get_notification(db, delivery.notification_id)
    if notification is None:
        _fail(delivery, reason="unknown notification")
        return
    delivery.attempts += 1
    try:
        subject, body = _email_content(notification)
        send_email(recipient.email, subject, body)
        delivery.status = DeliveryStatus.sent  # 'delivered' confirmed by the provider webhook
        delivery.next_attempt_at = None
        logger.info(
            "fr_18_03_success channel=email recipient=%s attempts=%s",
            delivery.recipient_id, delivery.attempts,
        )
    except Exception:  # noqa: BLE001 - any send error is retried with backoff, never dropped
        if delivery.attempts >= MAX_ATTEMPTS:
            _fail(delivery, reason="max attempts exhausted")
        else:
            delivery.status = DeliveryStatus.retrying
            delivery.next_attempt_at = _now() + backoff_delay(delivery.attempts)
            logger.warning(
                "fr_18_03_error channel=email recipient=%s attempts=%s next_attempt_at=%s",
                delivery.recipient_id, delivery.attempts, delivery.next_attempt_at,
            )


def _fail(delivery: AlertDelivery, *, reason: str) -> None:
    delivery.status = DeliveryStatus.failed
    delivery.next_attempt_at = None
    # CRITICAL, surfaced to ops per DoD — fields: flag_id, recipient_id, attempts (no PII/payload).
    logger.critical(
        "fr_18_03_delivery_failed flag_id=%s recipient_id=%s attempts=%s reason=%s",
        delivery.flag_id, delivery.recipient_id, delivery.attempts, reason,
    )


def confirm_delivery(
    db: Session, notification_id: uuid.UUID, delivered: bool
) -> AlertDelivery | None:
    """Provider webhook: confirm the email delivery. Legal only from `sent`; idempotent on terminal.

    Lifecycle: queued -> sent -> delivered | failed | retrying. A webhook on a row that was never
    `sent` (still queued/retrying) is out of order and is ignored (no illegal skip to delivered).
    """
    delivery = billing_db.get_email_delivery_for_notification(db, notification_id)
    if delivery is None:
        return None
    if delivery.status in (DeliveryStatus.delivered, DeliveryStatus.failed):
        return delivery  # terminal states are idempotent; a late webhook can't flip them
    if delivery.status != DeliveryStatus.sent:
        return delivery  # never-sent (queued/retrying): reject the skip, leave state unchanged
    if delivered:
        delivery.status = DeliveryStatus.delivered
        delivery.delivered_at = _now()
    else:
        delivery.status = DeliveryStatus.failed
    billing_db.persist(db)
    return delivery


def feed_for(
    db: Session, recipient_id: uuid.UUID
) -> list[tuple[Notification, list[AlertDelivery]]]:
    """Each notification the recipient owns, paired with its per-channel deliveries.

    Carries the payload/context and every channel's status, so a failed/retrying EMAIL delivery is
    visible on the in-app surface (SC-054), never a bare notice.
    """
    notifications = billing_db.feed_notifications(db, recipient_id)
    by_notification: dict[uuid.UUID, list[AlertDelivery]] = {}
    for delivery in billing_db.deliveries_for_recipient(db, recipient_id):
        by_notification.setdefault(delivery.notification_id, []).append(delivery)
    return [(n, by_notification.get(n.id, [])) for n in notifications]


def enqueue_to_school(
    db: Session,
    sender_school_id: uuid.UUID | None,
    recipient_id: uuid.UUID,
    ntype: str,
    payload: dict[str, Any] | None,
) -> Notification:
    """Enqueue to an IN-SCHOOL recipient only (403 otherwise). Returns 202 without a blocking send.

    Delivery happens out-of-band via the worker (process_due_deliveries) — the producer is never
    blocked by a slow/failing email provider, and never processes another school's backlog.
    """
    recipient = identity_db.get_staff(db, recipient_id)
    if recipient is None or recipient.school_id != sender_school_id:
        logger.info(
            "fr_18_03_forbidden reason=recipient_not_in_school recipient=%s school=%s",
            recipient_id, sender_school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Recipient not in your school")
    notification = enqueue(db, recipient_id=recipient_id, ntype=ntype, payload=payload)
    logger.info(
        "fr_18_03_success action=enqueued notification=%s recipient=%s",
        notification.id, recipient_id,
    )
    return notification
