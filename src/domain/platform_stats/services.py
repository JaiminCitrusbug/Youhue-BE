"""Platform-stats domain services — DB access only (FR-19-07). Plain COUNTs over existing tables;
this module owns no new schema/state, just the read queries."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import SubscriptionState
from src.domain.billing.models import Subscription
from src.domain.checkin.models import CheckIn
from src.domain.identity.models import School
from src.domain.risk.models import Flag


def count_schools(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(School)) or 0


def count_active_trials(db: Session, now: datetime) -> int:
    """A trial counts as ACTIVE only once armed AND running (FR-17-03 / GATE G-10): `state=trial`,
    `trial_start_at` set (the first check-in fired), and `trial_end_at` not yet passed. An approved
    school with no check-in yet (armed, not counting down) is correctly excluded."""
    return (
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.state == SubscriptionState.trial,
                Subscription.trial_start_at.is_not(None),
                Subscription.trial_end_at.is_not(None),
                Subscription.trial_end_at >= now,
            )
        )
        or 0
    )


def count_checkins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(CheckIn)) or 0


def count_flags(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(Flag)) or 0
