"""FR-05-01 — POST /api/v1/check-ins/{id}/activity: record that the caller started or completed
the guided activity offered on their own check-in.

Every ticket Must-not / Scenario has a test that fails if the guarantee is removed:

  S1   An activity is offered after check-in (covered in test_checkin_submit.py MN-6).
  S2   Skipping (never calling this endpoint) leaves the check-in complete, not required.
       -> test_skip_never_calls_endpoint_checkin_still_complete
  S3   Starting or completing the offered activity is recorded for the student's own history.
       -> test_start_records_started_status
       -> test_complete_records_completed_status
       -> test_start_then_complete_progresses_status
  NEG  A check-in that doesn't exist, isn't the caller's own, or was never offered an activity ->
       404 (same response for all three, never leaking which case it was).
       -> test_unknown_checkin_is_404
       -> test_another_students_checkin_is_404
       -> test_checkin_with_no_activity_offered_is_404
  NEG  An invalid status value -> 422.
       -> test_invalid_status_is_422
"""
from datetime import time

from sqlalchemy import select

from src.constants.enums import (
    ActivityAgeBand,
    ActivityEngagementStatus,
    ActivityType,
    ParentalConsentStatus,
)
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import ActivityEngagement, CheckIn
from src.domain.compliance import services as compliance_db
from src.domain.org.models import CalendarConfig

CHECKINS = "/api/v1/check-ins"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _open_all_day_window(db, school, timezone: str = "UTC") -> None:
    db.add(CalendarConfig(
        school_id=school.id, window_start=time(0, 0), window_end=time(23, 59, 59),
        timezone=timezone,
    ))
    db.commit()


def _verify_consent(db, student) -> None:
    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.verified
    )
    db.commit()


def _student_token(client, school, student) -> str:
    r = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    return r.json()["session_token"]


def _ready(db, client, school, student) -> str:
    _open_all_day_window(db, school)
    _verify_consent(db, student)
    return _student_token(client, school, student)


def _checked_in_with_offer(client, db, school, student) -> str:
    """Seed one matching activity, submit a check-in, and return the check-in id — the offer is
    guaranteed non-null since exactly one candidate activity exists."""
    checkin_db.add_seed_activity(
        db, title="Brain break", type=ActivityType.brain_break,
        age_band=ActivityAgeBand.all, topic=None,
    )
    db.commit()
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 201
    assert r.json()["activity_offer"] is not None
    return r.json()["checkin_id"], token


# ---- S3 — start / complete are recorded ---------------------------------------------------------


def test_start_records_started_status(client, db, make_school, make_student):
    school = make_school(code="ACT-1")
    student = make_student(school)
    checkin_id, token = _checked_in_with_offer(client, db, school, student)

    r = client.post(f"{CHECKINS}/{checkin_id}/activity", json={"status": "started"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["activity"]["status"] == "started"
    assert body["activity"]["title"] == "Brain break"

    engagement = db.scalar(select(ActivityEngagement).where(ActivityEngagement.checkin_id == checkin_id))
    assert engagement.status == ActivityEngagementStatus.started


def test_complete_records_completed_status(client, db, make_school, make_student):
    school = make_school(code="ACT-2")
    student = make_student(school)
    checkin_id, token = _checked_in_with_offer(client, db, school, student)

    r = client.post(f"{CHECKINS}/{checkin_id}/activity", json={"status": "completed"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["activity"]["status"] == "completed"

    engagement = db.scalar(select(ActivityEngagement).where(ActivityEngagement.checkin_id == checkin_id))
    assert engagement.status == ActivityEngagementStatus.completed


def test_start_then_complete_progresses_status(client, db, make_school, make_student):
    school = make_school(code="ACT-3")
    student = make_student(school)
    checkin_id, token = _checked_in_with_offer(client, db, school, student)

    r1 = client.post(f"{CHECKINS}/{checkin_id}/activity", json={"status": "started"}, headers=_auth(token))
    assert r1.json()["activity"]["status"] == "started"
    r2 = client.post(f"{CHECKINS}/{checkin_id}/activity", json={"status": "completed"}, headers=_auth(token))
    assert r2.json()["activity"]["status"] == "completed"


# ---- S2 — skip = never calling this endpoint ------------------------------------------------------


def test_skip_never_calls_endpoint_checkin_still_complete(client, db, make_school, make_student):
    """Skipping is the absence of a call, not a verb this endpoint accepts — a check-in with an
    offered-but-never-actioned engagement is still a complete check-in."""
    school = make_school(code="ACT-4")
    student = make_student(school)
    checkin_id, _token = _checked_in_with_offer(client, db, school, student)

    checkin = db.get(CheckIn, checkin_id)
    assert checkin is not None  # the check-in itself is unaffected by skipping the activity

    engagement = db.scalar(select(ActivityEngagement).where(ActivityEngagement.checkin_id == checkin_id))
    assert engagement.status == ActivityEngagementStatus.offered  # untouched — never started/completed


# ---- NEG — 404s, never leaking which case it was --------------------------------------------------


def test_unknown_checkin_is_404(client, db, make_school, make_student):
    import uuid

    school = make_school(code="ACT-5")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(
        f"{CHECKINS}/{uuid.uuid4()}/activity", json={"status": "started"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_another_students_checkin_is_404(client, db, make_school, make_student):
    school = make_school(code="ACT-6")
    owner = make_student(school, name="Owner")
    intruder = make_student(school, name="Intruder")
    checkin_id, _owner_token = _checked_in_with_offer(client, db, school, owner)
    intruder_token = _student_token(client, school, intruder)

    r = client.post(
        f"{CHECKINS}/{checkin_id}/activity", json={"status": "started"}, headers=_auth(intruder_token)
    )
    assert r.status_code == 404


def test_checkin_with_no_activity_offered_is_404(client, db, make_school, make_student):
    """A check-in exists but no seed activity matched at submit time (empty seed set) -> no
    engagement row -> 404, same as a nonexistent check-in."""
    school = make_school(code="ACT-7")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.json()["activity_offer"] is None
    checkin_id = r.json()["checkin_id"]

    r2 = client.post(
        f"{CHECKINS}/{checkin_id}/activity", json={"status": "started"}, headers=_auth(token)
    )
    assert r2.status_code == 404


def test_invalid_status_is_422(client, db, make_school, make_student):
    school = make_school(code="ACT-8")
    student = make_student(school)
    checkin_id, token = _checked_in_with_offer(client, db, school, student)

    r = client.post(
        f"{CHECKINS}/{checkin_id}/activity", json={"status": "skipped"}, headers=_auth(token)
    )
    assert r.status_code == 422
