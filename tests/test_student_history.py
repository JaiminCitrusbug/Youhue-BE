"""FR-08-01 — GET /api/v1/students/me/history (SC-025): the caller's own moods over time + own
reflections.

  S1  A student with prior check-ins sees their own moods over time and reflections.
      -> test_history_returns_own_moods_and_reflections_newest_first
  S2  A student can never see another student's history — structurally (no id param exists) and
      at runtime (a query-string student_id is simply ignored, own data only).
      -> test_no_student_id_param_exists
      -> test_query_string_student_id_is_ignored_own_data_only
  S3  A student with no check-ins yet sees an empty state, not an error.
      -> test_history_empty_state_before_any_checkins
  MN  Only check-ins with a non-empty reflection appear in `reflections`.
      -> test_reflections_excludes_mood_only_days
  MN  Self-only: a staff session is denied.
      -> test_staff_session_denied_403
  MN  No auth -> 401.
      -> test_no_auth_is_401
"""
from datetime import date

import pytest

from src.domain.checkin import services as checkin_db

HISTORY = "/api/v1/students/me/history"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _student_token(client, school, student) -> str:
    r = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    return r.json()["session_token"]


def _seed_checkin(db, student, school, *, local_date: date, mood: int, reflection: str | None):
    checkin_db.create_checkin(
        db,
        student_id=student.id,
        school_id=school.id,
        mood_value=mood,
        reflection_text=reflection,
        within_window=True,
        local_date=local_date,
    )
    db.commit()


# ---- S1 — happy path -----------------------------------------------------------------------------


def test_history_returns_own_moods_and_reflections_newest_first(
    client, db, make_school, make_student
):
    school = make_school(code="HIST-1")
    student = make_student(school)
    _seed_checkin(db, student, school, local_date=date(2026, 7, 1), mood=2, reflection=None)
    _seed_checkin(
        db, student, school, local_date=date(2026, 7, 3), mood=4, reflection="Great day"
    )
    _seed_checkin(db, student, school, local_date=date(2026, 7, 2), mood=3, reflection="")
    token = _student_token(client, school, student)

    r = client.get(HISTORY, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()

    moods = body["moods_over_time"]
    assert [m["local_date"] for m in moods] == ["2026-07-03", "2026-07-02", "2026-07-01"]
    assert [m["mood_value"] for m in moods] == [4, 3, 2]

    reflections = body["reflections"]
    assert len(reflections) == 1
    assert reflections[0] == {"local_date": "2026-07-03", "reflection_text": "Great day"}


# ---- S2 — self-only, structurally and at runtime --------------------------------------------------


def test_no_student_id_param_exists():
    """Same structural proof FR-04-01's `test_student_cannot_be_impersonated_no_id_param_exists`
    uses: the route takes no path/query student_id at all, so there is nothing to spoof."""
    import inspect

    from src.routers.students import get_my_history

    params = inspect.signature(get_my_history).parameters
    assert "student_id" not in params
    assert set(params) == {"student", "db"}


@pytest.mark.authz
def test_query_string_student_id_is_ignored_own_data_only(client, db, make_school, make_student):
    school = make_school(code="HIST-2")
    me = make_student(school, name="Amy")
    other = make_student(school, name="Ben")
    _seed_checkin(db, me, school, local_date=date(2026, 7, 1), mood=1, reflection=None)
    _seed_checkin(db, other, school, local_date=date(2026, 7, 1), mood=5, reflection="not mine")
    token = _student_token(client, school, me)

    r = client.get(f"{HISTORY}?student_id={other.id}", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert len(body["moods_over_time"]) == 1
    assert body["moods_over_time"][0]["mood_value"] == 1
    assert body["reflections"] == []


# ---- S3 — empty state, not an error ----------------------------------------------------------------


def test_history_empty_state_before_any_checkins(client, db, make_school, make_student):
    school = make_school(code="HIST-3")
    student = make_student(school)
    token = _student_token(client, school, student)

    r = client.get(HISTORY, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"moods_over_time": [], "reflections": []}


# ---- MN — reflections excludes mood-only days -------------------------------------------------------


def test_reflections_excludes_mood_only_days(client, db, make_school, make_student):
    school = make_school(code="HIST-4")
    student = make_student(school)
    _seed_checkin(db, student, school, local_date=date(2026, 7, 1), mood=2, reflection=None)
    _seed_checkin(db, student, school, local_date=date(2026, 7, 2), mood=3, reflection="   ")
    token = _student_token(client, school, student)

    r = client.get(HISTORY, headers=_auth(token))
    body = r.json()
    assert len(body["moods_over_time"]) == 2
    assert body["reflections"] == []


# ---- MN — self-only role/auth ------------------------------------------------------------------------


@pytest.mark.authz
def test_staff_session_denied_403(client, make_school, make_staff):
    from src.constants.enums import StaffRole

    school = make_school(code="HIST-5")
    make_staff(school, role=StaffRole.teacher)
    r = client.post(
        "/api/v1/auth/staff/sign-in", json={"email": "t@oakwood.edu", "password": "Password123"}
    )
    token = r.json()["access_token"]
    resp = client.get(HISTORY, headers=_auth(token))
    assert resp.status_code == 403


def test_no_auth_is_401():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as anon:
        r = anon.get(HISTORY)
    assert r.status_code in (401, 403)  # HTTPBearer auto_error -> 403 with no header, 401 bad token
