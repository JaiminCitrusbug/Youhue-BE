"""FR-07-03 — the check-in access-window guard: `require_within_access_window` (ticket §NEG,
GATE G-3 negative). NOT wired to any production caller yet (`POST /api/v1/check-ins` is FR-04-01,
which does not exist in this codebase yet) — these tests prove the guard function itself works, per
the ticket's ask, exactly like `test_consent.py` proves FR-20-06's `require_verified_consent` guard.

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  A check-in inside the configured window on a school day is allowed.
        -> test_allows_inside_window_on_school_day
  MN-2  Before window_start is blocked, 403 "check-in is not open right now".
        -> test_blocks_before_window_start
  MN-3  After window_end is blocked, 403 "check-in is not open right now".
        -> test_blocks_after_window_end
  MN-4  A configured holiday/half-term blocks even when the time-of-day would otherwise be inside
        the window.
        -> test_blocks_on_holiday_even_inside_window
  MN-5  Comparison is done in the school's OWN configured timezone, never server-local/UTC.
        -> test_comparison_uses_school_timezone_not_utc
  MN-6  No CalendarConfig row yet for the school -> blocked (no silent allow, no silent UTC
        fallback) — same documented posture FR-07-04 uses for calendar-period resolution.
        -> test_blocks_when_no_calendar_config
  MN-7  A corrupted/invalid stored timezone is a genuine server error (500), not a silent pass.
        -> test_errors_on_corrupt_stored_timezone
"""
from datetime import UTC, date, datetime, time

import pytest
from fastapi import HTTPException

from src.application.checkin import services as guard
from src.domain.org.models import CalendarConfig


def _set_config(
    db, school, *, timezone: str = "Europe/London",
    window_start: time = time(8, 30), window_end: time = time(9, 30),
    holidays: list[date] | None = None,
) -> CalendarConfig:
    row = CalendarConfig(
        school_id=school.id, window_start=window_start, window_end=window_end,
        timezone=timezone, holidays=holidays,
    )
    db.add(row)
    db.commit()
    return row


# =================================================================================================
# Positive — inside the window on a school day
# =================================================================================================


def test_allows_inside_window_on_school_day(db, make_school):
    school = make_school(code="CKW-1")
    _set_config(db, school, timezone="UTC", window_start=time(8, 0), window_end=time(9, 0))
    at = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)  # a Wednesday, mid-window
    assert guard.require_within_access_window(db, school.id, at=at) is None


def test_allows_exactly_at_window_boundaries(db, make_school):
    """Window comparison is inclusive at both ends."""
    school = make_school(code="CKW-2")
    _set_config(db, school, timezone="UTC", window_start=time(8, 0), window_end=time(9, 0))
    assert guard.require_within_access_window(
        db, school.id, at=datetime(2026, 7, 22, 8, 0, tzinfo=UTC)
    ) is None
    assert guard.require_within_access_window(
        db, school.id, at=datetime(2026, 7, 22, 9, 0, tzinfo=UTC)
    ) is None


def test_allows_defaults_to_now_when_at_omitted(db, make_school, monkeypatch):
    school = make_school(code="CKW-3")
    _set_config(db, school, timezone="UTC", window_start=time(0, 0), window_end=time(23, 59, 59))

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return cls(2026, 7, 22, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(guard, "datetime", _FixedDatetime)
    assert guard.require_within_access_window(db, school.id) is None


# =================================================================================================
# Negative — outside the window
# =================================================================================================


def test_blocks_before_window_start(db, make_school):
    school = make_school(code="CKW-4")
    _set_config(db, school, timezone="UTC", window_start=time(8, 30), window_end=time(9, 30))
    at = datetime(2026, 7, 22, 8, 0, tzinfo=UTC)  # 30 min before window opens
    with pytest.raises(HTTPException) as exc:
        guard.require_within_access_window(db, school.id, at=at)
    assert exc.value.status_code == 403
    assert exc.value.detail == "check-in is not open right now"


def test_blocks_after_window_end(db, make_school):
    school = make_school(code="CKW-5")
    _set_config(db, school, timezone="UTC", window_start=time(8, 30), window_end=time(9, 30))
    at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)  # 30 min after window closes
    with pytest.raises(HTTPException) as exc:
        guard.require_within_access_window(db, school.id, at=at)
    assert exc.value.status_code == 403
    assert exc.value.detail == "check-in is not open right now"


# =================================================================================================
# Negative — holiday / half-term
# =================================================================================================


def test_blocks_on_holiday_even_inside_window(db, make_school):
    """MN-4: the time-of-day is squarely inside the window, but the DATE is a configured holiday."""
    school = make_school(code="CKW-6")
    _set_config(
        db, school, timezone="UTC", window_start=time(8, 0), window_end=time(9, 0),
        holidays=[date(2026, 7, 22)],
    )
    at = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)
    with pytest.raises(HTTPException) as exc:
        guard.require_within_access_window(db, school.id, at=at)
    assert exc.value.status_code == 403


def test_allows_on_a_different_day_than_the_holiday(db, make_school):
    """A holiday list entry blocks only ITS OWN date — the guard still opens on ordinary days."""
    school = make_school(code="CKW-7")
    _set_config(
        db, school, timezone="UTC", window_start=time(8, 0), window_end=time(9, 0),
        holidays=[date(2026, 12, 25)],
    )
    at = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)
    assert guard.require_within_access_window(db, school.id, at=at) is None


# =================================================================================================
# Timezone — comparison is in the SCHOOL'S timezone, never server-local/UTC
# =================================================================================================


def test_comparison_uses_school_timezone_not_utc(db, make_school):
    """08:30 UTC is 09:30 in Europe/London during BST (+1) in July — squarely outside an
    08:00-09:00 LOCAL window if (and only if) the comparison is done in the school's own timezone,
    not against the raw UTC instant."""
    school = make_school(code="CKW-8")
    _set_config(db, school, timezone="Europe/London", window_start=time(8, 0), window_end=time(9, 0))
    at = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)  # 09:30 Europe/London -> outside 08:00-09:00
    with pytest.raises(HTTPException) as exc:
        guard.require_within_access_window(db, school.id, at=at)
    assert exc.value.status_code == 403


def test_comparison_naive_input_treated_as_utc_then_localized(db, make_school):
    """A caller-supplied `at` with no tzinfo is anchored as UTC first, then converted to the
    school's configured timezone — never interpreted as ambiguous server wall-clock time."""
    school = make_school(code="CKW-9")
    _set_config(db, school, timezone="Europe/London", window_start=time(9, 0), window_end=time(10, 0))
    at = datetime(2026, 7, 22, 8, 30)  # naive; == 08:30 UTC == 09:30 Europe/London (BST) -> inside
    assert guard.require_within_access_window(db, school.id, at=at) is None


def test_different_school_timezones_give_different_outcomes_for_the_same_instant(db, make_school):
    """MN-5 as a two-tenant proof: the SAME instant is inside one school's window and outside
    another's, purely because of each school's configured timezone."""
    school_utc = make_school(code="CKW-10A")
    _set_config(db, school_utc, timezone="UTC", window_start=time(8, 0), window_end=time(9, 0))
    school_ny = make_school(code="CKW-10B")
    _set_config(
        db, school_ny, timezone="America/New_York", window_start=time(8, 0), window_end=time(9, 0)
    )
    at = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)  # 08:30 UTC == 04:30 EDT
    assert guard.require_within_access_window(db, school_utc.id, at=at) is None
    with pytest.raises(HTTPException):
        guard.require_within_access_window(db, school_ny.id, at=at)


# =================================================================================================
# Configuration edge cases
# =================================================================================================


def test_blocks_when_no_calendar_config(db, make_school):
    """MN-6: no CalendarConfig row saved yet for this school — refused, not silently allowed."""
    school = make_school(code="CKW-11")
    with pytest.raises(HTTPException) as exc:
        guard.require_within_access_window(db, school.id, at=datetime(2026, 7, 22, 8, 30, tzinfo=UTC))
    assert exc.value.status_code == 403


def test_errors_on_corrupt_stored_timezone(db, make_school):
    """A hand-edited/corrupted timezone string is a genuine 500, never a silent pass-through."""
    school = make_school(code="CKW-12")
    row = CalendarConfig(
        school_id=school.id, window_start=time(8, 0), window_end=time(9, 0),
        timezone="Not/ARealZone",
    )
    db.add(row)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        guard.require_within_access_window(db, school.id, at=datetime(2026, 7, 22, 8, 30, tzinfo=UTC))
    assert exc.value.status_code == 500


def test_holidays_none_does_not_crash(db, make_school):
    """`holidays` is a nullable column — a school that never set any must not crash the guard."""
    school = make_school(code="CKW-13")
    _set_config(db, school, timezone="UTC", window_start=time(8, 0), window_end=time(9, 0),
                holidays=None)
    at = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)
    assert guard.require_within_access_window(db, school.id, at=at) is None
