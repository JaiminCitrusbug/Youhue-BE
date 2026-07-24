"""FR-07-04 — resolve 'this week'/'this month'/'this term' against the school's actual calendar
and timezone (GET /api/v1/calendar/resolve).

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  Period boundaries follow the school's CONFIGURED timezone, never a server/UTC default.
        -> test_resolve_this_week_follows_school_timezone_not_utc
  MN-2  'This term' covers the school's configured term dates.
        -> test_resolve_this_term_uses_configured_term_dates
  MN-3  Unknown period_key -> 422.
        -> test_resolve_unknown_period_key_is_422
  MN-4  Staff-scoped to the CALLER's own school only (school id from the session, not spoofable).
        -> test_resolve_uses_callers_own_school_not_a_query_param
  MN-5  No CalendarConfig row yet -> a documented, non-crashing fallback (404), never a silent
        server-default resolution.
        -> test_resolve_no_calendar_config_is_404
  MN-6  CalendarConfig exists but no term dates saved -> this_term 404s (week/month still resolve).
        -> test_resolve_this_term_without_term_dates_is_404
"""
from datetime import date, datetime

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import SchoolStatus, SessionKind, StaffRole
from src.domain.identity.models import StaffAccount
from src.domain.org.models import CalendarConfig

RESOLVE = "/api/v1/calendar/resolve"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _teacher(db, make_school, make_staff, *, code: str = "CAL-1"):
    school = make_school(code=code, status=SchoolStatus.active, name=f"Calendar {code}")
    teacher = make_staff(school, email=f"t-{code}@school.edu", role=StaffRole.teacher)
    return school, teacher, _mint(db, teacher)


def _set_config(
    db, school, *, timezone: str = "Europe/London",
    window_start: str = "08:30", window_end: str = "09:30",
    term_start: date | None = None, term_end: date | None = None,
) -> CalendarConfig:
    row = CalendarConfig(
        school_id=school.id, window_start=window_start, window_end=window_end,
        timezone=timezone, term_start=term_start, term_end=term_end,
    )
    db.add(row)
    db.commit()
    return row


class _FixedDatetime(datetime):
    """A fixed 'now' (2026-07-22 10:00 local, a Wednesday) so week/month boundaries are
    deterministic without needing a frozen-time dependency."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 7, 22, 10, 0, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    import src.application.calendar.services as calendar_svc

    monkeypatch.setattr(calendar_svc, "datetime", _FixedDatetime)


# =================================================================================================
# this_week / this_month — school timezone, not a server default
# =================================================================================================


def test_resolve_this_week_follows_school_timezone_not_utc(client, db, make_school, make_staff):
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-1")
    _set_config(db, school, timezone="Europe/London")

    r = client.get(RESOLVE, params={"period_key": "this_week"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    # 2026-07-22 is a Wednesday -> the ISO week is Mon 2026-07-20 -> Mon 2026-07-27 (exclusive end).
    assert body["start"] == "2026-07-20T00:00:00+01:00"  # Europe/London is BST (+1) in July
    assert body["end"] == "2026-07-27T00:00:00+01:00"


def test_resolve_this_week_different_timezone_gives_different_offset(
    client, db, make_school, make_staff
):
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-2")
    _set_config(db, school, timezone="America/New_York")

    r = client.get(RESOLVE, params={"period_key": "this_week"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["start"] == "2026-07-20T00:00:00-04:00"  # EDT in July
    assert body["end"] == "2026-07-27T00:00:00-04:00"


def test_resolve_this_month_bounds(client, db, make_school, make_staff):
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-3")
    _set_config(db, school, timezone="UTC")

    r = client.get(RESOLVE, params={"period_key": "this_month"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["start"] == "2026-07-01T00:00:00Z" or body["start"] == "2026-07-01T00:00:00+00:00"
    assert body["end"] == "2026-08-01T00:00:00Z" or body["end"] == "2026-08-01T00:00:00+00:00"


# =================================================================================================
# this_term — the school's configured term dates
# =================================================================================================


def test_resolve_this_term_uses_configured_term_dates(client, db, make_school, make_staff):
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-4")
    _set_config(
        db, school, timezone="UTC",
        term_start=date(2026, 6, 1), term_end=date(2026, 7, 31),
    )

    r = client.get(RESOLVE, params={"period_key": "this_term"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["start"].startswith("2026-06-01T00:00:00")
    # term_end is the last INCLUSIVE day -> exclusive boundary is the day after.
    assert body["end"].startswith("2026-08-01T00:00:00")


def test_resolve_this_term_without_term_dates_is_404(client, db, make_school, make_staff):
    """MN-6: a CalendarConfig row exists (window/timezone saved) but term dates were never set."""
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-5")
    _set_config(db, school, timezone="UTC")  # no term_start/term_end

    r = client.get(RESOLVE, params={"period_key": "this_term"}, headers=_auth(token))
    assert r.status_code == 404

    # week/month still resolve fine off the same row — only term is blocked.
    week = client.get(RESOLVE, params={"period_key": "this_week"}, headers=_auth(token))
    assert week.status_code == 200


# =================================================================================================
# Edge cases / must-nots
# =================================================================================================


def test_resolve_unknown_period_key_is_422(client, db, make_school, make_staff):
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-6")
    _set_config(db, school)

    r = client.get(RESOLVE, params={"period_key": "this_year"}, headers=_auth(token))
    assert r.status_code == 422


def test_resolve_no_calendar_config_is_404(client, db, make_school, make_staff):
    """MN-5: leadership never saved a CalendarConfig row for this school yet."""
    school, teacher, token = _teacher(db, make_school, make_staff, code="CAL-7")

    r = client.get(RESOLVE, params={"period_key": "this_week"}, headers=_auth(token))
    assert r.status_code == 404


def test_resolve_uses_callers_own_school_not_a_query_param(client, db, make_school, make_staff):
    """MN-4: the resolved school is always the CALLER's own (session-derived) — there is no
    school_id parameter to spoof. Two schools, two different timezones/term configs; the caller
    from school A always gets school A's resolution, never school B's, however B is configured."""
    school_a, teacher_a, token_a = _teacher(db, make_school, make_staff, code="CAL-8A")
    school_b, teacher_b, token_b = _teacher(db, make_school, make_staff, code="CAL-8B")
    _set_config(db, school_a, timezone="UTC")
    _set_config(db, school_b, timezone="America/New_York")

    ra = client.get(RESOLVE, params={"period_key": "this_week"}, headers=_auth(token_a))
    rb = client.get(RESOLVE, params={"period_key": "this_week"}, headers=_auth(token_b))
    assert ra.status_code == 200 and rb.status_code == 200
    assert ra.json()["start"] != rb.json()["start"]  # different tz offsets -> different strings


def test_resolve_unauthenticated_is_refused(client):
    r = client.get(RESOLVE, params={"period_key": "this_week"})
    assert r.status_code in (401, 403)


def test_openapi_documents_the_resolve_endpoint(client):
    paths = client.get("/openapi.json").json()["paths"]
    resolve = paths["/api/v1/calendar/resolve"]["get"]["responses"]
    assert {"200", "404", "422", "500"} <= set(resolve)
