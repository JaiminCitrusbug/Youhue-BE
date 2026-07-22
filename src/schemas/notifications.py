"""Notification request/response schemas."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class EnqueueRequest(BaseModel):
    recipient_id: uuid.UUID
    type: str
    payload: dict[str, Any] | None = None


class DeliveryCallback(BaseModel):
    delivered: bool


class DeliveryOut(BaseModel):
    """Per-channel delivery status — so a failed/retrying EMAIL is visible on the in-app feed."""

    channel: str
    status: str


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    payload: dict[str, Any] | None = None
    created_at: datetime
    deliveries: list[DeliveryOut]
