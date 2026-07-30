"""FR-17-04: Subscription lifecycle action responses (downgrade / mid-term cancellation)."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from src.constants.enums import SubscriptionState, SubscriptionTier


class SubscriptionActionResponse(BaseModel):
    """200 body for both `POST /subscriptions/{id}/downgrade` and `.../cancel`. The ticket's own
    DoD literal shape for downgrade is `{state: 'free'}` — this response is a superset (still
    contains `state`) so the resulting `tier`/`term_end_at` are visible too, never guessed by the
    caller from a bare string."""

    id: uuid.UUID
    school_id: uuid.UUID
    tier: SubscriptionTier
    state: SubscriptionState
    term_end_at: datetime | None = None
