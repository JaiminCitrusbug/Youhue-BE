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

from src.constants.enums import ActivityAgeBand, ActivityScope, ActivityType
from src.domain.checkin.models import Activity, CheckIn, CheckInSettings


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


def list_checkins_for_student(db: Session, student_id: uuid.UUID) -> list[CheckIn]:
    """FR-08-01 — every check-in `student_id` has ever submitted, newest first. Scoped by
    `student_id` alone (indexed, `ix_checkins_student_id`) — the caller (`StudentDep`) has already
    resolved this to the SIGNED-IN student's own id off the session; there is no student_id
    parameter anywhere in the request, so this can never be called with another student's id."""
    return list(
        db.scalars(
            select(CheckIn)
            .where(CheckIn.student_id == student_id)
            .order_by(CheckIn.local_date.desc(), CheckIn.submitted_at.desc())
        )
    )


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
    captured_offline: bool = False,
    client_entry_id: str | None = None,
) -> CheckIn:
    """`local_date` (school-LOCAL calendar day, caller-computed) backs
    `uq_checkins_student_local_date` — the DB-level backstop for "one check-in per student per day"
    (FR-04-01 review remediation, Finding 1: the read-then-write check alone loses a genuine
    concurrent race). The caller (`submit_checkin`/`sync_checkin`) is responsible for catching the
    resulting `IntegrityError` on a concurrent duplicate and translating it to the appropriate
    response (see each caller's own docstring).

    `client_entry_id` (FR-04-06, offline sync only) backs `ix_checkins_client_entry_id` — the
    DB-level backstop for the sync endpoint's idempotency guarantee, same reasoning as
    `local_date`/its constraint above: the read-then-write check in `sync_checkin` cannot see a
    genuinely concurrent second sync of the same offline entry."""
    row = CheckIn(
        student_id=student_id,
        school_id=school_id,
        mood_value=mood_value,
        reflection_text=reflection_text,
        within_window=within_window,
        local_date=local_date,
        captured_offline=captured_offline,
        client_entry_id=client_entry_id,
    )
    db.add(row)
    db.flush()
    return row


def get_checkin_by_client_entry_id(
    db: Session, student_id: uuid.UUID, client_entry_id: str
) -> CheckIn | None:
    """FR-04-06 — the offline sync idempotency lookup: has THIS student already synced an entry
    under this `client_entry_id`? Scoped by `student_id` too (not just the globally-unique
    `client_entry_id`) so a lookup can never leak or return another student's row even in a
    hypothetical client_entry_id collision across students (the client generates this ID, so a
    cross-student collision is not impossible the way a server-generated UUID's would be)."""
    return db.scalar(
        select(CheckIn).where(
            CheckIn.student_id == student_id, CheckIn.client_entry_id == client_entry_id
        )
    )


def list_checkins_for_students_between(
    db: Session, student_ids: list[uuid.UUID], start: datetime, end: datetime
) -> list[CheckIn]:
    """FR-10-01 — every check-in ANY of `student_ids` submitted in `[start, end)`; the caller (the
    dashboard's mood-index owner) has already resolved `student_ids` to the caller's own
    accessible class roster. Empty `student_ids` short-circuits to no query (a class with zero
    members is a valid, non-error state — an empty mood index, not a crash)."""
    if not student_ids:
        return []
    return list(
        db.scalars(
            select(CheckIn).where(
                CheckIn.student_id.in_(student_ids),
                CheckIn.submitted_at >= start,
                CheckIn.submitted_at < end,
            )
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


def bump_score_attempt(db: Session, checkin: CheckIn) -> int:
    """Record a failed scoring attempt; caller dead-letters once the bounded cap is reached."""
    checkin.score_attempts += 1
    db.flush()
    return checkin.score_attempts


# ---- FR-19-04: seed activity set (scope=seed only; school-scoped activities are Phase 2) ----

def add_seed_activity(
    db: Session,
    *,
    title: str,
    type: ActivityType,
    age_band: ActivityAgeBand,
    topic: str | None,
) -> Activity:
    """Insert a new platform seed activity (school_id is NULL — global to every school)."""
    activity = Activity(
        scope=ActivityScope.seed,
        school_id=None,
        title=title,
        type=type,
        age_band=age_band,
        topic=topic,
    )
    db.add(activity)
    db.flush()
    return activity


def get_seed_activity(db: Session, activity_id: uuid.UUID) -> Activity | None:
    """Fetch a single seed-scoped activity by id; a school-scoped id resolves to None here."""
    return db.scalars(
        select(Activity).where(
            Activity.id == activity_id, Activity.scope == ActivityScope.seed
        )
    ).one_or_none()


def list_seed_activities(db: Session, *, include_retired: bool = False) -> list[Activity]:
    """List seed activities, active-first by title. Retired (active=False) excluded by default —
    the set FR-05-01/FR-14-02 consume is the active subset."""
    stmt = select(Activity).where(Activity.scope == ActivityScope.seed)
    if not include_retired:
        stmt = stmt.where(Activity.active.is_(True))
    return list(db.scalars(stmt.order_by(Activity.title)))
