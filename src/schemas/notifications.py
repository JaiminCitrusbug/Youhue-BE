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
    # FR-18-01 (SC-054 notifications centre): null = unread. Set once by mark-all-read; the FE
    # derives the unread dot / count from this, never a separate invented flag.
    read_at: datetime | None = None
    deliveries: list[DeliveryOut]


class MarkAllReadOut(BaseModel):
    marked: int
