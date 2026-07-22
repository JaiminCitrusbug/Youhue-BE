"""Check-in domain services — DB access only."""
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain.checkin.models import CheckIn


def get_checkins_since(db: Session, student_id: uuid.UUID, since: datetime) -> list[CheckIn]:
    return list(
        db.scalars(
            select(CheckIn).where(CheckIn.student_id == student_id, CheckIn.submitted_at >= since)
        )
    )


def get_unscored_for_update(db: Session) -> list[CheckIn]:
    return list(
        db.scalars(select(CheckIn).where(CheckIn.scored.is_(False)).with_for_update(skip_locked=True))
    )


def get_checkin(db: Session, checkin_id: uuid.UUID) -> CheckIn | None:
    return db.get(CheckIn, checkin_id)


def mark_scored(db: Session, checkin: CheckIn) -> None:
    checkin.scored = True
    db.flush()
