"""Account lockout business rule: N consecutive failures within the window locks the identifier."""
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from config.env_config import settings
from src.domain.auth import services as auth_db


def record_attempt(db: Session, identifier: str, succeeded: bool) -> None:
    auth_db.add_login_attempt(db, identifier, succeeded)


def is_locked(db: Session, identifier: str, max_attempts: int | None = None) -> bool:
    limit = max_attempts if max_attempts is not None else settings.lockout_max_attempts
    window_start = datetime.now(UTC) - timedelta(minutes=settings.lockout_window_minutes)
    consecutive_failures = 0
    for attempt in auth_db.recent_attempts(db, identifier, window_start):
        if attempt.succeeded:
            break
        consecutive_failures += 1
    return consecutive_failures >= limit
