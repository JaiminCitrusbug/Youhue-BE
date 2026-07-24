"""Check-in domain services — DB access only.

`CheckIn` itself (id, mood_value, reflection_text, submitted_at, within_window, ...) was created by
INFRA-02 (`8ab7f8057e0a`) — FR-04-01 is its first WRITER (`create_checkin` below), not its creator.
`CheckInSettings` is new here (see `src.domain.checkin.models` docstring) — FR-04-01's "require a
reflection" school setting.
"""
import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.domain.checkin.models import CheckIn, CheckInSettings


def get_checkins_since(db: Session, student_id: uuid.UUID, since: datetime) -> list[CheckIn]:
    return list(
        db.scalars(
            select(CheckIn).where(CheckIn.student_id == student_id, CheckIn.submitted_at >= since)
        )
    )


# ---- FR-04-01: settings + writer ---------------------------------------------------------------


def get_checkin_settings(db: Session, school_id: uuid.UUID) -> CheckInSettings | None:
    """A school's "require a reflection" setting, if it has ever saved one. `None` -> optional
    (the caller's default), same "no row yet = least-restrictive" posture FR-16-02 uses
    elsewhere."""
    return db.scalar(select(CheckInSettings).where(CheckInSettings.school_id == school_id))


def set_checkin_settings(
    db: Session, school_id: uuid.UUID, *, require_reflection: bool
) -> CheckInSettings:
    """Upsert the one settings row per school. No production caller ships in this ticket (the
    leadership-facing write surface is out of scope — see `docs/DEFERRALS.md`); tests call this
    directly, and any future hub extension would call it the same way FR-16-02's
    `_apply_access_window` calls `org_db.set_calendar_config`."""
    row = get_checkin_settings(db, school_id)
    if row is None:
        row = CheckInSettings(school_id=school_id, require_reflection=require_reflection)
        db.add(row)
    else:
        row.require_reflection = require_reflection
    db.flush()
    return row


def list_checkins_for_student_between(
    db: Session, student_id: uuid.UUID, start: datetime, end: datetime
) -> list[CheckIn]:
    """Every check-in a student submitted in `[start, end)` — the caller computes the range as the
    school's own local-timezone day boundary (never a raw UTC calendar day)."""
    return list(
        db.scalars(
            select(CheckIn).where(
                CheckIn.student_id == student_id,
                CheckIn.submitted_at >= start,
                CheckIn.submitted_at < end,
            )
        )
    )


def create_checkin(
    db: Session,
    *,
    student_id: uuid.UUID,
    school_id: uuid.UUID,
    mood_value: int,
    reflection_text: str | None,
    within_window: bool,
    local_date: date,
) -> CheckIn:
    """`local_date` (school-LOCAL calendar day, caller-computed) backs
    `uq_checkins_student_local_date` — the DB-level backstop for "one check-in per student per day"
    (FR-04-01 review remediation, Finding 1: the read-then-write check alone loses a genuine
    concurrent race). The caller (`submit_checkin`) is responsible for catching the resulting
    `IntegrityError` on a concurrent duplicate and translating it to the same 409 the sequential
    duplicate path already returns."""
    row = CheckIn(
        student_id=student_id,
        school_id=school_id,
        mood_value=mood_value,
        reflection_text=reflection_text,
        within_window=within_window,
        local_date=local_date,
    )
    db.add(row)
    db.flush()
    return row


def get_unscored_for_update(db: Session) -> list[CheckIn]:
    return list(
        db.scalars(select(CheckIn).where(CheckIn.scored.is_(False)).with_for_update(skip_locked=True))
    )


def get_checkin(db: Session, checkin_id: uuid.UUID) -> CheckIn | None:
    return db.get(CheckIn, checkin_id)


def mark_scored(db: Session, checkin: CheckIn) -> None:
    checkin.scored = True
    db.flush()


def bump_score_attempt(db: Session, checkin: CheckIn) -> int:
    """Record a failed scoring attempt; caller dead-letters once the bounded cap is reached."""
    checkin.score_attempts += 1
    db.flush()
    return checkin.score_attempts
