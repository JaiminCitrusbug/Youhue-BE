"""Notification endpoints (INFRA-05). Enqueue is in-school-scoped; the feed is per-recipient."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from src.application import notifications as notif
from src.config import settings
from src.infrastructure.models.identity import StaffAccount
from src.interfaces.deps import DbDep, StaffDep

router = APIRouter(prefix="/notifications", tags=["notifications"])


class EnqueueRequest(BaseModel):
    recipient_id: uuid.UUID
    type: str
    payload: dict[str, Any] | None = None


class DeliveryCallback(BaseModel):
    delivered: bool


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    channel: str
    delivery_status: str


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def enqueue_notification(body: EnqueueRequest, staff: StaffDep, db: DbDep) -> dict[str, str]:
    recipient = db.get(StaffAccount, body.recipient_id)
    if recipient is None or recipient.school_id != staff.school_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Recipient not in your school")
    rows = notif.enqueue(db, recipient_id=body.recipient_id, ntype=body.type, payload=body.payload)
    notif.dispatch_pending(
        db
    )  # attempt delivery now (the background worker also drains the outbox)
    db.commit()
    return {"status": "accepted", "count": str(len(rows))}


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
    if x_webhook_secret != settings.sendgrid_webhook_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid webhook signature")
    n = notif.confirm_delivery(db, notification_id, body.delivered)
    db.commit()
    return {"status": n.delivery_status.value if n else "unknown"}


@router.get("", response_model=list[NotificationOut])
def list_feed(staff: StaffDep, db: DbDep) -> list[NotificationOut]:
    return [
        NotificationOut(
            id=n.id, type=n.type, channel=n.channel.value, delivery_status=n.delivery_status.value
        )
        for n in notif.feed_for(db, staff.id)
    ]
