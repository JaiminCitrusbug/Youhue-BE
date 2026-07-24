"""FR-07-04 — resolve 'this week' / 'this month' / 'this term' against the school's ACTUAL
calendar + timezone (SC-063's config, FR-16-02). This is a pure READ/resolution service — it
does not own writing ``CalendarConfig`` (that stays FR-16-02's ``application.leadership.services``);
it only reads ``CalendarConfig.timezone`` / ``.term_start`` / ``.term_end`` and computes period
boundaries from them.

Staff-scoped to the CALLER's OWN school only: the school id is read off the authenticated
``StaffAccount`` (``staff.school_id``), never accepted as a request parameter, so it cannot be
spoofed cross-tenant (ticket §Interaction contract).

Every period's ``start``/``end`` follows the school's CONFIGURED IANA timezone
(``CalendarConfig.timezone``) via the stdlib ``zoneinfo`` — never a server/UTC default — matching
the ``zoneinfo`` usage FR-16-02 already established for this same config (validated at write time
in ``application.leadership.services._apply_access_window``).

Edge case (ticket-acknowledged: FR-16-02 does not force every school to configure a window):
no ``CalendarConfig`` row yet, or (for ``this_term`` only) a row with no term dates saved yet ->
404, never a silent UTC/server-default fallback that would produce a plausible-looking but WRONG
figure. This is the documented fallback decision for this ticket (see ticket §DoD "decide and log
a sensible fallback").

Structured logs: ``fr_07_04_success`` / ``_rejected`` / ``_forbidden`` / ``_error`` (SRS §K).
"""
import logging
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.domain.identity.models import StaffAccount
from src.domain.org import services as org_db

logger = logging.getLogger("youhue.calendar")

PERIOD_KEYS = ("this_week", "this_month", "this_term")


def _week_bounds(today: date) -> tuple[date, date]:
    """Monday->Sunday (ISO week). End is EXCLUSIVE (next Monday)."""
    start = today - timedelta(days=today.weekday())
    return start, start + timedelta(days=7)


def _month_bounds(today: date) -> tuple[date, date]:
    """1st of the month -> 1st of the next month. End is EXCLUSIVE."""
    start = today.replace(day=1)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def resolve_period(
    db: Session, staff: StaffAccount, period_key: str
) -> tuple[datetime, datetime]:
    school_id = staff.school_id  # staff-scoped to the CALLER's own school only — never a param

    if period_key not in PERIOD_KEYS:
        logger.warning(
            "fr_07_04_rejected action=resolve actor_id=%s reason=unknown_period_key "
            "period_key=%s", staff.id, period_key,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Unknown period_key — must be one of {', '.join(PERIOD_KEYS)}",
        )

    config = org_db.get_calendar_config(db, school_id)
    if config is None:
        # FR-16-02 does not force every school to configure a window immediately — this is a
        # real, expected state, not a system error. Documented fallback decision: refuse clearly
        # (404) rather than silently resolving against a server/UTC default that would look
        # plausible but be WRONG for this school.
        logger.info(
            "fr_07_04_rejected action=resolve actor_id=%s school_id=%s "
            "reason=no_calendar_config period_key=%s", staff.id, school_id, period_key,
        )
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "Calendar/timezone is not configured for this school yet",
        )

    try:
        tz = ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # Should not happen — FR-16-02 validates the timezone at write time — but a corrupted or
        # hand-edited row must never crash unhandled; it is a genuine server-side error.
        logger.error(
            "fr_07_04_error action=resolve actor_id=%s school_id=%s "
            "reason=bad_stored_timezone timezone=%s", staff.id, school_id, config.timezone,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Calendar configuration is invalid"
        ) from exc

    today = datetime.now(tz).date()

    if period_key == "this_week":
        start_date, end_date = _week_bounds(today)
    elif period_key == "this_month":
        start_date, end_date = _month_bounds(today)
    else:  # this_term
        if config.term_start is None or config.term_end is None:
            logger.info(
                "fr_07_04_rejected action=resolve actor_id=%s school_id=%s "
                "reason=no_term_dates", staff.id, school_id,
            )
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                "Term dates are not configured for this school yet",
            )
        start_date = config.term_start
        end_date = config.term_end + timedelta(days=1)  # term_end is the last INCLUSIVE day

    start_dt = datetime.combine(start_date, time.min, tzinfo=tz)
    end_dt = datetime.combine(end_date, time.min, tzinfo=tz)

    logger.info(
        "fr_07_04_success action=resolve actor_id=%s school_id=%s period_key=%s "
        "start=%s end=%s",
        staff.id, school_id, period_key, start_dt.isoformat(), end_dt.isoformat(),
    )
    return start_dt, end_dt
