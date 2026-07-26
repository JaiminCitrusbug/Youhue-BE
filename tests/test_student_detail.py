"""FR-10-02 — student detail drill-in (GET /api/v1/students/{id}/detail).

  Scenario 1  A teacher drills from the class dashboard into a single student and sees mood
              history, reflections and participation.  -> test_detail_returns_history_and_participation
  Scenario 2  Participation = check-ins completed / check-ins expected in the window.
              -> test_participation_is_completed_over_expected
  NEG         Out-of-scope student (own school, different/unshared class) -> 403.
              -> test_403_for_student_outside_own_or_shared_scope
  NEG         Cross-school student -> 403 (audited), never 404 (existence hidden across tenants).
              -> test_403_for_cross_school_student
  NEG         A genuinely unknown student id -> 404 (distinct from the 403 cases above).
              -> test_404_for_unknown_student
"""
from datetime import datetime

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import SessionKind, StaffClassScope, StaffRole
from src.domain.checkin.models import CheckIn
from src.domain.identity.models import StaffAccount
from src.domain.org.models import CalendarConfig

DETAIL = "/api/v1/students/{id}/detail"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _set_config(db, school, *, timezone: str = "UTC") -> CalendarConfig:
    row = CalendarConfig(school_id=school.id, window_start="08:00", window_end="09:00",
                          timezone=timezone)
    db.add(row)
    db.commit()
    return row


def _checkin(db, student, school, *, mood: int, at: datetime, reflection: str | None = None):
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=mood,
        submitted_at=at, local_date=at.date(), reflection_text=reflection,
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


class _FixedDatetime(datetime):
    """2026-07-22 10:00 UTC (a Wednesday) — matches test_dashboard.py's fixed-now pattern so
    'this_week' (Mon 2026-07-20 .. Mon 2026-07-27 excl., 7 days) is deterministic."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls(2026, 7, 22, 10, 0, 0, tzinfo=tz)


@pytest.fixture(autouse=True)
def _fixed_now(monkeypatch):
    import src.application.calendar.services as calendar_svc

    monkeypatch.setattr(calendar_svc, "datetime", _FixedDatetime)


def test_detail_returns_history_and_participation(
    db, client, make_school, make_staff, make_class, make_student, grant_class_access, add_to_class
):
    school = make_school(code="SD-1")
    _set_config(db, school)
    teacher = make_staff(school, email="t@sd1.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3A")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Amy")
    add_to_class(klass, student)
    _checkin(db, student, school, mood=4, at=datetime(2026, 7, 21, 9, 0), reflection="Good day")

    token = _mint(db, teacher)
    r = client.get(DETAIL.format(id=student.id), headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mood_history"] == [{"local_date": "2026-07-21", "mood_value": 4}]
    assert body["reflections"] == [{"local_date": "2026-07-21", "reflection_text": "Good day"}]
    assert body["participation_rate"] == round(1 / 7, 4)


def test_participation_is_completed_over_expected(
    db, client, make_school, make_staff, make_class, make_student, grant_class_access, add_to_class
):
    school = make_school(code="SD-2")
    _set_config(db, school)
    teacher = make_staff(school, email="t@sd2.edu", role=StaffRole.teacher)
    klass = make_class(school, name="3B")
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    student = make_student(school, name="Ben")
    add_to_class(klass, student)
    # 3 check-ins inside the resolved this-week window (Mon..Sun, 7 expected days).
    _checkin(db, student, school, mood=3, at=datetime(2026, 7, 20, 9, 0))
    _checkin(db, student, school, mood=4, at=datetime(2026, 7, 21, 9, 0))
    _checkin(db, student, school, mood=2, at=datetime(2026, 7, 22, 9, 0))

    token = _mint(db, teacher)
    r = client.get(DETAIL.format(id=student.id), headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["participation_rate"] == round(3 / 7, 4)


def test_403_for_student_outside_own_or_shared_scope(
    db, client, make_school, make_staff, make_class, make_student
):
    school = make_school(code="SD-3")
    _set_config(db, school)
    teacher = make_staff(school, email="t@sd3.edu", role=StaffRole.teacher)
    other_class = make_class(school, name="Other")  # teacher has NO access to this class
    student = make_student(school, name="Cam")
    from src.domain.org.models import ClassMembership

    db.add(ClassMembership(class_id=other_class.id, student_id=student.id))
    db.commit()

    token = _mint(db, teacher)
    r = client.get(DETAIL.format(id=student.id), headers=_auth(token))
    assert r.status_code == 403


def test_403_for_cross_school_student(db, client, make_school, make_staff, make_student):
    school_a = make_school(code="SD-4A")
    school_b = make_school(code="SD-4B")
    _set_config(db, school_a)
    teacher = make_staff(school_a, email="t@sd4.edu", role=StaffRole.teacher)
    other_student = make_student(school_b, name="Cross")

    token = _mint(db, teacher)
    r = client.get(DETAIL.format(id=other_student.id), headers=_auth(token))
    assert r.status_code == 403  # never 404 — existence across tenants stays hidden


def test_404_for_unknown_student(db, client, make_school, make_staff):
    import uuid

    school = make_school(code="SD-5")
    _set_config(db, school)
    teacher = make_staff(school, email="t@sd5.edu", role=StaffRole.teacher)

    token = _mint(db, teacher)
    r = client.get(DETAIL.format(id=uuid.uuid4()), headers=_auth(token))
    assert r.status_code == 404
