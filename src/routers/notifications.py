"""Notification endpoints (INFRA-05). Enqueue is in-school-scoped; the feed is per-recipient.
Thin router — business logic in src.application.notifications.services."""
import hmac
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, status

from config.env_config import settings
from src.application.notifications import services as notif
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.notifications import DeliveryCallback, DeliveryOut, EnqueueRequest, NotificationOut

router = APIRouter(prefix="/notifications", tags=["notifications"])
logger = logging.getLogger("youhue.notifications")


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def enqueue_notification(body: EnqueueRequest, staff: StaffDep, db: DbDep) -> dict[str, str]:
    notification = notif.enqueue_to_school(
        db, staff.school_id, body.recipient_id, body.type, body.payload
    )
    db.commit()
    return {"status": "accepted", "notification_id": str(notification.id)}


@router.post("/{notification_id}/delivery")
def delivery_callback(
    notification_id: uuid.UUID,
    body: DeliveryCallback,
    db: DbDep,
    x_webhook_secret: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    # Provider webhook (SendGrid) — authenticated by a shared secret; idempotent.
    if not settings.sendgrid_webhook_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Delivery webhook not configured")
    # constant-time comparison — never leak the secret via timing.
    if x_webhook_secret is None or not hmac.compare_digest(
        x_webhook_secret, settings.sendgrid_webhook_secret
    ):
        logger.info("fr_18_03_rejected reason=bad_webhook_signature notif=%s", notification_id)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")
    delivery = notif.confirm_delivery(db, notification_id, body.delivered)
    db.commit()
    return {"status": delivery.status.value if delivery else "unknown"}


@router.get("", response_model=list[NotificationOut])
def list_feed(staff: StaffDep, db: DbDep) -> list[NotificationOut]:
    return [
        NotificationOut(
            id=n.id,
            type=n.type,
            payload=n.payload,
            created_at=n.created_at,
            deliveries=[
                DeliveryOut(channel=d.channel.value, status=d.status.value) for d in deliveries
            ],
        )
        for n, deliveries in notif.feed_for(db, staff.id)
    ]
