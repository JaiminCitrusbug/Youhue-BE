"""Notification request/response schemas."""
import uuid
from typing import Any

from pydantic import BaseModel


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
