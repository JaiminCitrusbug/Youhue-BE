"""FR-07-03 — the check-in access-window guard (SC-023 s4 blocked state): a check-in is accepted
only within the school's configured access window, and blocked outside it or on a holiday/half-term
(ticket §Interaction contract, GATE G-3 negative).

The consent-before-use gate (ticket §NEG): `require_within_access_window` is a REUSABLE guard other
tickets' write-code-paths call BEFORE proceeding — mirrors FR-20-06's `require_verified_consent`
pattern (`src/application/compliance/services.py`) exactly: a plain `(db, ...) -> None` function
that raises 403 to block and returns `None` to allow.

`POST /api/v1/check-ins` (the check-in submit endpoint this guard wraps) is FR-04-01 — a separate,
not-yet-existing ticket in this batch: nothing in this codebase calls this guard yet. It is shipped
here so FR-04-01 can wire it in first, before writing a `CheckIn` row, and record whichever branch
it took as `CheckIn.within_window` (the guard's "returns None" path is the only path that reaches a
write at all — a raise means the request never gets that far). It is NOT wired into any production
endpoint by this ticket — see the ticket report for the "awaiting a caller" framing; do not read
this as already-enforced end-to-end.

Timezone comparison REUSES FR-07-04's resolution machinery (`src/application/calendar/services.py`):
the same stdlib `zoneinfo` read of `CalendarConfig.timezone`, the same documented "no config yet"
refusal instead of a silent UTC/server-default fallback, and the same corrupted-timezone ->
500 handling — this module does not reimplement any of that timezone math from scratch, it composes
`src.domain.org.services.get_calendar_config` exactly like `resolve_period` does.

Structured logs: `fr_07_03_success` / `_rejected` / `_forbidden` / `_error` (ticket §DoD, SRS §K).
"""
import logging
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.compliance import services as compliance_svc
from src.constants.enums import StudentAgeBand
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import CheckIn
from src.domain.identity.models import Student
from src.domain.org import services as org_db

logger = logging.getLogger("youhue.checkin")

_NOT_OPEN = "check-in is not open right now"
_REFLECTION_REQUIRED = "A reflection is required before this check-in can be completed"
_INVALID_MOOD = "Invalid mood value for your age group"
_ALREADY_CHECKED_IN = "You already checked in today"


def _to_school_local(at: datetime | None, tz: ZoneInfo) -> datetime:
    """`at` defaults to "now"; when given, an aware value is converted to the school's timezone and
    a naive value is treated as UTC first (never as ambiguous server-local wall-clock time) — same
    "never a server/UTC default for the COMPARISON, but UTC is a safe neutral anchor for an already
    naive caller-supplied instant" posture FR-07-04 documents for its own inputs."""
    if at is None:
        return datetime.now(tz)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(tz)


def require_within_access_window(
    db: Session, school_id: uuid.UUID, at: datetime | None = None
) -> None:
    """THE check-in access-window guard (ticket §Interaction contract / GATE G-3 negative): raise
    `HTTPException(403, "check-in is not open right now")` unless `at` (default: now) falls within
    the school's configured `CalendarConfig.window_start`/`window_end` on a day that is NOT listed
    in `CalendarConfig.holidays` — compared entirely in the school's OWN configured timezone, never
    server-local. Returns `None` (does not raise) when the check-in is allowed.

    A school with no `CalendarConfig` row yet cannot have an open window — refused (403), same
    "no silent default" posture as FR-07-04, logged distinctly (`_rejected`, a configuration state)
    from a genuine window/holiday timing block (`_forbidden`).
    """
    config = org_db.get_calendar_config(db, school_id)
    if config is None:
        logger.info(
            "fr_07_03_rejected action=require_within_access_window school_id=%s "
            "reason=no_calendar_config", school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NOT_OPEN)

    try:
        tz = ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # Should not happen — FR-16-02 validates the timezone at write time — but a corrupted or
        # hand-edited row must never crash unhandled; it is a genuine server-side error, not a
        # timing/config-absence rejection.
        logger.error(
            "fr_07_03_error action=require_within_access_window school_id=%s "
            "reason=bad_stored_timezone timezone=%s", school_id, config.timezone,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Calendar configuration is invalid"
        ) from exc

    local_now = _to_school_local(at, tz)
    today = local_now.date()
    time_now = local_now.time()

    holidays = config.holidays or []
    if today in holidays:
        logger.warning(
            "fr_07_03_forbidden action=require_within_access_window school_id=%s "
            "reason=holiday date=%s", school_id, today,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NOT_OPEN)

    if not (config.window_start <= time_now <= config.window_end):
        logger.warning(
            "fr_07_03_forbidden action=require_within_access_window school_id=%s "
            "reason=outside_window time=%s window_start=%s window_end=%s",
            school_id, time_now, config.window_start, config.window_end,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, _NOT_OPEN)

    logger.info(
        "fr_07_03_success action=require_within_access_window school_id=%s date=%s time=%s",
        school_id, today, time_now,
    )
    return None


# =================================================================================================
# FR-04-01 — POST /api/v1/check-ins: record a student's own daily mood check-in (+ reflection).
#
# Guard order (ticket): `require_within_access_window` runs FIRST, before any write (this module's
# own guard above); a raise there propagates as a 403 and this function adds its own
# `fr_04_01_forbidden` log around it, on top of the guard's internal `fr_07_03_*` logging.
#
# COPPA consent-before-use (FR-20-06) — WIRED here, deliberately. FR-20-06's own ticket text
# names the caller explicitly: "Depends on: INFRA-02 [+ the COPPA/FERPA posture governs FR-04-01
# check-in data use]". A daily mood check-in (plus an optional free-text reflection) is squarely
# "child data" under COPPA, and it is the FIRST student-authored data-use path in this codebase
# reaching production (sign-in captures no data beyond identity/roster lookups already covered by
# INFRA-02's model). `require_verified_consent` raises its own 403 (with its own `fr_20_06_*`
# logging) when a student's consent is `pending` or has no record at all — this function does not
# duplicate that logging, only re-raises. Contrast with FR-02-02's Subscription "arm not started"
# deferral (a genuinely LATER ticket's feature): consent is not a later ticket here, it is this
# ticket's own first real data-use path, so wiring it now (rather than deferring) is the correct
# call, not scope creep.
# =================================================================================================


def _parse_mood_set(raw: str) -> list[int]:
    return [int(v.strip()) for v in raw.split(",") if v.strip()]


def mood_set_for_band(age_band: StudentAgeBand) -> list[int]:
    """FR-04-01 — the age-appropriate mood set, carried as env/config (ticket Q-3: "an assumption
    pending client confirmation... carry the set as configuration, do not hard-code contents as a
    product decision"). Mirrors the established pattern for other placeholder-pending-ratification
    data in this codebase (`settings.concern_words`)."""
    raw = {
        StudentAgeBand.b5_7: settings.mood_set_b5_7,
        StudentAgeBand.b8_11: settings.mood_set_b8_11,
        StudentAgeBand.b12_18: settings.mood_set_b12_18,
    }[age_band]
    return _parse_mood_set(raw)


def get_mood_set(student: Student) -> list[int]:
    """The CALLER's own age-appropriate mood set — GET /api/v1/check-ins/mood-set reads this."""
    return mood_set_for_band(student.age_band)


def submit_checkin(
    db: Session, student: Student, mood_value: int, reflection_text: str | None
) -> tuple[CheckIn, bool]:
    """Record `student`'s own check-in for today (their own school's local day). Returns
    `(row, created)` — `created=False` on an exact-content retry (see the idempotency note below).

    Order: access-window guard -> consent-before-use guard -> mood validity -> reflection-required
    setting -> one-per-day rule -> write. Every reject path raises before any row is written.

    Idempotency reading (ticket: "all writes ACID + idempotent" [BR-05]): a RETRY of the *exact
    same* submission (same `mood_value` AND same `reflection_text` as today's existing row) returns
    the SAME 201 success referencing the existing row — a network retry of a request that already
    landed must not surface as an error. Any OTHER second submission on the same day (a different
    mood or reflection — a genuinely new choice, not a retry) is a hard 409: "one check-in per
    student per day" is a domain invariant, not merely a replay-safety concern, so a deliberate
    second submission with different content is rejected, not silently overwritten.
    """
    try:
        require_within_access_window(db, student.school_id)
    except HTTPException:
        logger.warning(
            "fr_04_01_forbidden action=submit_checkin student_id=%s reason=access_window",
            student.id,
        )
        raise

    # FR-20-06 consent-before-use gate — see module note above. Raises its own 403 + fr_20_06_*
    # log; nothing to add here beyond letting it propagate.
    compliance_svc.require_verified_consent(db, student.id)

    allowed = mood_set_for_band(student.age_band)
    if mood_value not in allowed:
        logger.info(
            "fr_04_01_rejected action=submit_checkin student_id=%s reason=invalid_mood mood=%s",
            student.id, mood_value,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _INVALID_MOOD)

    settings_row = checkin_db.get_checkin_settings(db, student.school_id)
    reflection_required = settings_row.require_reflection if settings_row is not None else False
    has_reflection = bool(reflection_text and reflection_text.strip())
    if reflection_required and not has_reflection:
        logger.info(
            "fr_04_01_rejected action=submit_checkin student_id=%s reason=reflection_required",
            student.id,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, _REFLECTION_REQUIRED)

    # Reachable only because require_within_access_window above already required a CalendarConfig
    # row to exist (a missing row raises 403 there) — this is a defensive re-fetch of the SAME row,
    # not a fresh possibility; the 500 below documents the invariant rather than being live-tested.
    config = org_db.get_calendar_config(db, student.school_id)
    if config is None:  # pragma: no cover
        logger.error(
            "fr_04_01_error action=submit_checkin student_id=%s reason=no_calendar_config",
            student.id,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Check-in configuration is invalid"
        )

    tz = ZoneInfo(config.timezone)
    local_now = _to_school_local(None, tz)
    day_start = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    day_end = day_start + timedelta(days=1)

    existing_rows = checkin_db.list_checkins_for_student_between(
        db, student.id, day_start.astimezone(UTC), day_end.astimezone(UTC)
    )
    if existing_rows:
        existing = existing_rows[0]
        same_mood = existing.mood_value == mood_value
        same_reflection = (existing.reflection_text or "") == (reflection_text or "")
        if same_mood and same_reflection:
            logger.info(
                "fr_04_01_success action=submit_checkin student_id=%s checkin_id=%s "
                "reason=idempotent_replay", student.id, existing.id,
            )
            return existing, False
        logger.info(
            "fr_04_01_rejected action=submit_checkin student_id=%s reason=duplicate_day", student.id
        )
        raise HTTPException(status.HTTP_409_CONFLICT, _ALREADY_CHECKED_IN)

    row = checkin_db.create_checkin(
        db,
        student_id=student.id,
        school_id=student.school_id,
        mood_value=mood_value,
        reflection_text=reflection_text,
        within_window=True,  # only reachable because require_within_access_window returned None
    )
    logger.info(
        "fr_04_01_success action=submit_checkin student_id=%s checkin_id=%s", student.id, row.id
    )
    return row, True
