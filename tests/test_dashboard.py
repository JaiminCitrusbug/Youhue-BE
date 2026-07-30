"""FR-10-01 — class dashboard (GET /api/v1/classes/{id}/dashboard).

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  Weighted mood index + up/down trend vs the prior period, both server-owned.
        -> test_dashboard_returns_mood_index_and_trend_matching_an_independent_recompute
        -> test_trend_reports_up_and_down_directionally
  MN-2  Header states whose data, which period/timezone, and live/as-of.
        -> test_dashboard_header_states_whose_period_timezone_and_live
  MN-3  Teacher may read own/shared classes only — 403 otherwise (and for a nonexistent class,
        same response, never distinguishing the two).
        -> test_dashboard_403_for_class_outside_own_or_shared_scope
        -> test_dashboard_403_for_nonexistent_class_same_as_inaccessible
  MN-4  No check-ins yet -> null mood_index + flat trend, never a fabricated 0/direction.
        -> test_dashboard_no_checkins_returns_null_index_and_flat_trend
  MN-5  A shared-scope teacher (not just the owner) can read the dashboard.
        -> test_dashboard_readable_by_shared_scope_teacher_not_only_owner
  MN-6  (FR-10-05) `data_state` distinguishes `no_data_yet` (class NEVER checked in) from
        `no_results` (this window is empty but the class HAS check-ins elsewhere) — never
        interchangeable copy; a 500 renders as an error state, never a crash.
        -> test_dashboard_never_checked_in_returns_no_data_yet
        -> test_dashboard_filter_matches_nothing_though_data_exists_returns_no_results
        -> test_dashboard_data_state_is_has_data_when_mood_index_present
        -> test_dashboard_unexpected_error_renders_500_never_a_crash
        -> test_fr_10_05_success_rejected_forbidden_error_are_logged
"""
from datetime import date, datetime

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.application.dashboard import services as dashboard_svc
from src.constants.enums import SchoolStatus, SessionKind, StaffClassScope, StaffRole
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import CheckIn
from src.domain.identity.models import StaffAccount
from src.domain.org.models import CalendarConfig

DASH = "/api/v1/classes/{id}/dashboard"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _set_config(
    db, school, *, timezone: str = "UTC",
    term_start: date | None = None, term_end: date | None = None,
) -> CalendarConfig:
    row = CalendarConfig(
        school_id=school.id, window_start="08:00", window_end="09:00", timezone=timezone,
        term_start=term_start, term_end=term_end,
    )
    db.add(row)
    db.commit()
    return row


def _checkin(db, student, school, *, mood: int, at: datetime) -> CheckIn:
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=mood,
        submitted_at=at, local_date=at.date(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


class _FixedDatetime(datetime):
    """2026-07-22 10:00 UTC (a Wednesday) — matches test_calendar.py's fixed-now pattern so the
    'this_week' boundary (Mon 2026-07-20 .. Mon 2026-07-27 excl.) is deterministic."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 7, 22, 10, 0, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    import src.application.calendar.services as calendar_svc

    monkeypatch.setattr(calendar_svc, "datetime", _FixedDatetime)


def _setup_class_with_checkins(db, make_school, make_staff, make_student, make_class,
                                grant_class_access, add_to_class, *, code: str):
    school = make_school(code=code, status=SchoolStatus.active, name=f"Dash {code}")
    _set_config(db, school)
    teacher = make_staff(school, email=f"t-{code}@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    s1 = make_student(school, name="Amy")
    s2 = make_student(school, name="Ben")
    add_to_class(klass, s1)
    add_to_class(klass, s2)
    # current week (2026-07-20 .. 2026-07-27): moods 3, 5 -> mean 4.0 * scale 2.0 = 8.0
    _checkin(db, s1, school, mood=3, at=datetime(2026, 7, 21, 9, 0))
    _checkin(db, s2, school, mood=5, at=datetime(2026, 7, 21, 9, 0))
    # prior week (2026-07-13 .. 2026-07-20): moods 1, 1 -> mean 1.0 * scale 2.0 = 2.0
    _checkin(db, s1, school, mood=1, at=datetime(2026, 7, 14, 9, 0))
    _checkin(db, s2, school, mood=1, at=datetime(2026, 7, 15, 9, 0))
    return school, teacher, klass, _mint(db, teacher)


def test_dashboard_returns_mood_index_and_trend_matching_an_independent_recompute(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Scenario 1. The oracle here is DELIBERATELY independent of `compute_mood_index`: it re-reads
    the raw `CheckIn` rows directly and hand-computes the mean*scale itself, rather than calling the
    same registered owner function the endpoint calls (SRS §13.5 — a self-graded check is not a
    check)."""
    school, teacher, klass, token = _setup_class_with_checkins(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
        code="D-1",
    )
    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()

    # Independent oracle: hand-recompute from the raw rows this test itself inserted.
    current_moods = [3, 5]
    expected_index = round((sum(current_moods) / len(current_moods)) * settings.dashboard_mood_index_scale, 2)
    assert body["mood_index"] == expected_index == 8.0
    assert body["trend"] == "up"  # 8.0 > prior week's 2.0
    assert body["data_state"] == "has_data"  # FR-10-05: a populated mood_index is always has_data


def test_trend_reports_up_and_down_directionally(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """The SAME fixture data read from the opposite direction: if this test's class showed the
    prior-week data as its CURRENT week instead, trend must flip to 'down' — proves direction isn't
    hard-coded."""
    school = make_school(code="D-2", status=SchoolStatus.active, name="Dash D-2")
    _set_config(db, school)
    teacher = make_staff(school, email="t-d2@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3B")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Cara")
    add_to_class(klass, student)
    _checkin(db, student, school, mood=1, at=datetime(2026, 7, 21, 9, 0))   # current week: low
    _checkin(db, student, school, mood=5, at=datetime(2026, 7, 14, 9, 0))  # prior week: high
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["trend"] == "down"


def test_dashboard_header_states_whose_period_timezone_and_live(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Scenario 2."""
    school, teacher, klass, token = _setup_class_with_checkins(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
        code="D-3",
    )
    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    body = r.json()
    assert body["class_name"] == "3A"          # whose data
    assert body["period"] == "this_week"       # which period
    assert body["timezone"] == "UTC"           # which timezone
    assert body["live"] is True                # live vs as-of
    assert body["as_of"]                       # a real timestamp, not absent


def test_dashboard_403_for_class_outside_own_or_shared_scope(
    client, db, make_school, make_staff, make_class,
):
    school = make_school(code="D-4", status=SchoolStatus.active, name="Dash D-4")
    _set_config(db, school)
    teacher = make_staff(school, email="t-d4@school.edu", role=StaffRole.teacher)
    other_teacher = make_staff(school, email="other-d4@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="Not Mine")
    # `other_teacher` owns it; `teacher` has no access row at all.
    from src.constants.enums import StaffClassScope as _Scope
    from src.domain.org.models import StaffClassAccess
    db.add(StaffClassAccess(staff_id=other_teacher.id, class_id=klass.id, scope=_Scope.owner))
    db.commit()
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 403


def test_dashboard_403_for_nonexistent_class_same_as_inaccessible(
    client, db, make_school, make_staff,
):
    """A nonexistent class id must 403 exactly like an inaccessible one — never a distinguishing
    404 that would let a caller probe for other schools' class ids."""
    import uuid

    school = make_school(code="D-5", status=SchoolStatus.active, name="Dash D-5")
    _set_config(db, school)
    teacher = make_staff(school, email="t-d5@school.edu", role=StaffRole.teacher)
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=uuid.uuid4()), headers=_auth(token))
    assert r.status_code == 403


def test_dashboard_no_checkins_returns_null_index_and_flat_trend(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school = make_school(code="D-6", status=SchoolStatus.active, name="Dash D-6")
    _set_config(db, school)
    teacher = make_staff(school, email="t-d6@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="Empty")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Dee")
    add_to_class(klass, student)  # a real student, but zero check-ins this or last week, EVER
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["mood_index"] is None
    assert body["trend"] == "flat"
    # FR-10-05 Scenario 1: never having checked in at all is `no_data_yet`, not `no_results`.
    assert body["data_state"] == "no_data_yet"


def test_dashboard_readable_by_shared_scope_teacher_not_only_owner(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school = make_school(code="D-7", status=SchoolStatus.active, name="Dash D-7")
    _set_config(db, school)
    owner = make_staff(school, email="owner-d7@school.edu", role=StaffRole.teacher)
    co_teacher = make_staff(school, email="co-d7@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="Shared")
    grant_class_access(owner, klass, scope=StaffClassScope.owner)
    grant_class_access(co_teacher, klass, scope=StaffClassScope.shared)
    token = _mint(db, co_teacher)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200


ROSTER = "/api/v1/classes/{id}/roster"


def test_roster_lists_real_student_rows(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """FR-10-02: the dashboard's "click into a single student" (Scenario 1) needs real rows to
    link against — this endpoint returns the class's actual roster, name-ordered."""
    school = make_school(code="D-8", status=SchoolStatus.active, name="Dash D-8")
    teacher = make_staff(school, email="t-d8@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    ben = make_student(school, name="Ben")
    amy = make_student(school, name="Amy")
    add_to_class(klass, ben)
    add_to_class(klass, amy)
    token = _mint(db, teacher)

    r = client.get(ROSTER.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    names = [s["display_name"] for s in r.json()["students"]]
    assert names == ["Amy", "Ben"]  # name-ordered, not insertion-ordered


def test_roster_403_for_class_outside_own_or_shared_scope(client, db, make_school, make_staff, make_class):
    school = make_school(code="D-9", status=SchoolStatus.active, name="Dash D-9")
    teacher = make_staff(school, email="t-d9@school.edu", role=StaffRole.teacher)
    other_class = make_class(school, name="NotMine")
    token = _mint(db, teacher)

    r = client.get(ROSTER.format(id=other_class.id), headers=_auth(token))
    assert r.status_code == 403


# =================================================================================================
# FR-10-03 — time-range filter (range=this_week|month|term|around:{date})
# =================================================================================================


def test_range_omitted_defaults_to_this_week_unchanged(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Backward compatibility: FR-10-01's existing behaviour (no `range` param) is unchanged."""
    school, teacher, klass, token = _setup_class_with_checkins(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
        code="R-1",
    )
    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["period"] == "this_week"


def test_range_month_resolves_against_the_school_calendar(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Scenario 1: selecting 'month' updates the figures to cover that period, resolved via
    FR-07-04 (this test's fixed 'now' is 2026-07-22, so the month is July)."""
    school = make_school(code="R-2", status=SchoolStatus.active, name="Dash R-2")
    _set_config(db, school)
    teacher = make_staff(school, email="t-r2@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    _checkin(db, student, school, mood=4, at=datetime(2026, 7, 3, 9, 0))  # inside July, outside this_week
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id) + "?range=month", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "this_month"
    assert body["mood_index"] == round(4 * settings.dashboard_mood_index_scale, 2)


def test_range_term_resolves_against_configured_term_dates(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school = make_school(code="R-3", status=SchoolStatus.active, name="Dash R-3")
    _set_config(db, school, term_start=date(2026, 6, 1), term_end=date(2026, 8, 31))
    teacher = make_staff(school, email="t-r3@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    _checkin(db, student, school, mood=2, at=datetime(2026, 6, 15, 9, 0))  # in-term, outside this_week/month
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id) + "?range=term", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "this_term"
    assert body["mood_index"] == round(2 * settings.dashboard_mood_index_scale, 2)


def test_range_term_without_configured_term_dates_is_404(
    client, db, make_school, make_staff, make_class, grant_class_access,
):
    school = make_school(code="R-4", status=SchoolStatus.active, name="Dash R-4")
    _set_config(db, school)  # no term_start/term_end
    teacher = make_staff(school, email="t-r4@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id) + "?range=term", headers=_auth(token))
    assert r.status_code == 404


def test_range_around_a_specific_date_filters_to_the_week_containing_it(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Scenario 2: 'around' a date the teacher picks updates the figures to the range around
    that date — resolved to the week containing it, independent of the fixed 'now' (2026-07-22)."""
    school = make_school(code="R-5", status=SchoolStatus.active, name="Dash R-5")
    _set_config(db, school)
    teacher = make_staff(school, email="t-r5@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    # 2026-06-10 (a Wednesday) — its containing week is Mon 2026-06-08 .. Mon 2026-06-15 (excl).
    _checkin(db, student, school, mood=5, at=datetime(2026, 6, 10, 9, 0))
    _checkin(db, student, school, mood=1, at=datetime(2026, 7, 21, 9, 0))  # this_week — must be excluded
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id) + "?range=around:2026-06-10", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["period"] == "around"
    assert body["mood_index"] == round(5 * settings.dashboard_mood_index_scale, 2)


def test_range_invalid_value_is_422(client, db, make_school, make_staff, make_class, grant_class_access):
    school = make_school(code="R-6", status=SchoolStatus.active, name="Dash R-6")
    _set_config(db, school)
    teacher = make_staff(school, email="t-r6@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id) + "?range=nonsense", headers=_auth(token))
    assert r.status_code == 422


def test_range_around_with_unparseable_date_is_422(
    client, db, make_school, make_staff, make_class, grant_class_access,
):
    school = make_school(code="R-7", status=SchoolStatus.active, name="Dash R-7")
    _set_config(db, school)
    teacher = make_staff(school, email="t-r7@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id) + "?range=around:not-a-date", headers=_auth(token))
    assert r.status_code == 422


def test_changing_the_filter_refetches_never_recomputes_client_side(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Ticket §Must-nots: 'changing the filter re-fetches, it does not re-derive existing figures'
    — asserted at the contract level: two different `range` values against real, DIFFERENT
    underlying data return DIFFERENT server-computed figures, proving each call is a fresh
    resolution+aggregate, not a client-side transform of one cached response."""
    school = make_school(code="R-8", status=SchoolStatus.active, name="Dash R-8")
    _set_config(db, school)
    teacher = make_staff(school, email="t-r8@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    _checkin(db, student, school, mood=5, at=datetime(2026, 7, 21, 9, 0))  # inside this_week AND July
    _checkin(db, student, school, mood=1, at=datetime(2026, 7, 3, 9, 0))  # inside July only
    token = _mint(db, teacher)

    week = client.get(DASH.format(id=klass.id), headers=_auth(token)).json()
    month = client.get(DASH.format(id=klass.id) + "?range=month", headers=_auth(token)).json()
    assert week["mood_index"] == round(5 * settings.dashboard_mood_index_scale, 2)
    assert month["mood_index"] == round(((5 + 1) / 2) * settings.dashboard_mood_index_scale, 2)
    assert week["mood_index"] != month["mood_index"]


# =================================================================================================
# FR-10-05 — data_state (has_data|no_data_yet|no_results) + 500-never-a-crash + structured logs
# =================================================================================================


def test_dashboard_never_checked_in_returns_no_data_yet(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Scenario 1 (ticket, verbatim): a class with NO check-ins anywhere, for any period."""
    school = make_school(code="S-1", status=SchoolStatus.active, name="Dash S-1")
    _set_config(db, school)
    teacher = make_staff(school, email="t-s1@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)  # never checked in, ever
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["mood_index"] is None
    assert body["data_state"] == "no_data_yet"


def test_dashboard_filter_matches_nothing_though_data_exists_returns_no_results(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """Scenario 2 (ticket, verbatim): a filter that matches no check-ins although data EXISTS —
    the check-in here is real, just outside `this_week` (the default/current window)."""
    school = make_school(code="S-2", status=SchoolStatus.active, name="Dash S-2")
    _set_config(db, school)
    teacher = make_staff(school, email="t-s2@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    _checkin(db, student, school, mood=4, at=datetime(2026, 7, 3, 9, 0))  # inside July, outside this_week
    token = _mint(db, teacher)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))  # default range = this_week
    assert r.status_code == 200
    body = r.json()
    assert body["mood_index"] is None
    assert body["data_state"] == "no_results"  # distinct from no_data_yet — data exists, just not here


def test_dashboard_data_state_is_has_data_when_mood_index_present(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, klass, token = _setup_class_with_checkins(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
        code="S-3",
    )
    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["data_state"] == "has_data"


def test_dashboard_unexpected_error_renders_500_never_a_crash(
    client, db, make_school, make_staff, make_class, grant_class_access, monkeypatch,
):
    """Ticket §Must-nots: 'a server error (500) is rendered as an error state, never a crash, and
    is never silently dropped' — a genuinely unexpected exception deep in the read path still comes
    back as a real HTTP 500 response (TestClient would instead raise the exception if it were left
    unhandled), never a raw traceback leaked to the client."""
    school = make_school(code="S-4", status=SchoolStatus.active, name="Dash S-4")
    _set_config(db, school)
    teacher = make_staff(school, email="t-s4@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    token = _mint(db, teacher)

    def _boom(*_a: object, **_k: object) -> list[CheckIn]:
        raise RuntimeError("db went away")

    monkeypatch.setattr(checkin_db, "list_checkins_for_students_between", _boom)

    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 500
    assert r.json()["detail"] == "Could not load the dashboard"  # never a raw stack trace


def test_fr_10_05_success_rejected_forbidden_error_are_logged(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    monkeypatch,
):
    """Ticket DoD (verbatim): 'Logs emit fr_10_05_success / _rejected / _forbidden / _error.'
    `caplog` cannot be used in this suite (documented pre-existing quirk — `alembic/env.py`'s
    `logging.config.fileConfig` disables every `youhue.*` logger at collection time; see
    tests/test_roster.py:296-302) — monkeypatch the logger methods directly instead, same
    established workaround as test_roster.py/test_roster_reconcile.py."""
    school = make_school(code="S-5", status=SchoolStatus.active, name="Dash S-5")
    _set_config(db, school)
    teacher = make_staff(school, email="t-s5@school.edu", role=StaffRole.teacher)
    other_teacher = make_staff(school, email="other-s5@school.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    token = _mint(db, teacher)
    other_token = _mint(db, other_teacher)

    info_calls: list[str] = []
    warn_calls: list[str] = []
    error_calls: list[str] = []
    monkeypatch.setattr(
        dashboard_svc.logger, "info", lambda msg, *a, **k: info_calls.append(msg % a if a else msg)
    )
    monkeypatch.setattr(
        dashboard_svc.logger, "warning", lambda msg, *a, **k: warn_calls.append(msg % a if a else msg)
    )
    monkeypatch.setattr(
        dashboard_svc.logger, "error", lambda msg, *a, **k: error_calls.append(msg % a if a else msg)
    )

    # success
    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 200
    assert any("fr_10_05_success" in c for c in info_calls)

    # rejected (422 — invalid range)
    r = client.get(DASH.format(id=klass.id) + "?range=nonsense", headers=_auth(token))
    assert r.status_code == 422
    assert any("fr_10_05_rejected" in c for c in info_calls)

    # forbidden (403 — not this teacher's class)
    r = client.get(DASH.format(id=klass.id), headers=_auth(other_token))
    assert r.status_code == 403
    assert any("fr_10_05_forbidden" in c for c in warn_calls)

    # error (500 — unexpected exception). `dashboard_svc.logger` and the router's `logger` are the
    # SAME `logging.getLogger("youhue.dashboard")` singleton, so patching this one attribute covers
    # the router's `logger.error("fr_10_05_error"...)` call too.
    monkeypatch.setattr(
        dashboard_svc,
        "get_class_dashboard",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = client.get(DASH.format(id=klass.id), headers=_auth(token))
    assert r.status_code == 500
    assert any("fr_10_05_error" in c for c in error_calls)
