"""Account lockout: N consecutive failed sign-ins within the window locks the identifier (423)."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.infrastructure.models.auth import LoginAttempt


def record_attempt(db: Session, identifier: str, succeeded: bool) -> None:
    db.add(LoginAttempt(identifier=identifier, succeeded=succeeded))
    db.flush()


def is_locked(db: Session, identifier: str, max_attempts: int | None = None) -> bool:
    limit = max_attempts if max_attempts is not None else settings.lockout_max_attempts
    window_start = datetime.now(UTC) - timedelta(minutes=settings.lockout_window_minutes)
    stmt = (
        select(LoginAttempt)
        .where(LoginAttempt.identifier == identifier, LoginAttempt.at >= window_start)
        .order_by(LoginAttempt.at.desc())
    )
    consecutive_failures = 0
    for attempt in db.scalars(stmt):
        if attempt.succeeded:
            break
        consecutive_failures += 1
    return consecutive_failures >= limit
