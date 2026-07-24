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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.domain.org import services as org_db

logger = logging.getLogger("youhue.checkin")

_NOT_OPEN = "check-in is not open right now"


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
