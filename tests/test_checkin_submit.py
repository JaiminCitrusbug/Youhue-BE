"""FR-04-01 — POST /api/v1/check-ins (mood + optional/required reflection).
FR-04-03 — GET /check-ins/config (supersedes FR-04-01's original GET /mood-set).

Every ticket Must-not / Scenario has a test that fails if the guarantee is removed:

  S1   A student records their own check-in by selecting a mood -> 201.
       -> test_submit_mood_only_success
  S2   A reflection is optional and, when written, is saved with the check-in.
       -> test_submit_with_optional_reflection_is_saved
  S3   A school that requires a reflection blocks a mood-only submit (422) until one is written.
       -> test_reflection_required_blocks_without_reflection
       -> test_reflection_required_allows_with_reflection
  MN-1 Outside the access window -> 403 (FR-07-03 guard wired FIRST, before any write).
       -> test_outside_access_window_is_403_and_writes_nothing
  MN-2 Consent-before-use (FR-20-06): a pending/absent consent blocks the check-in -> 403.
       -> test_unverified_consent_is_403
       -> test_no_consent_record_at_all_is_403
  MN-3 One check-in per student per day: a genuinely different second submission -> 409; an EXACT
       retry of the same content is idempotent (same 201, no second row).
       -> test_duplicate_day_different_content_is_409
       -> test_exact_retry_is_idempotent_201
  MN-4 Mood value outside the caller's age-banded set -> 422.
       -> test_mood_outside_bands_set_is_422
  MN-5 Self-only: a student session carries no student_id param; a staff session is denied.
       -> test_student_cannot_be_impersonated_no_id_param_exists
       -> test_staff_session_denied_403
  MN-6 activity_offer (FR-05-01, closes DEF-010): null when no seed activity matches the caller's
       age band; a real offer (+ an `offered` engagement row) when one does; null again on an
       idempotent replay (never a second offer for the same check-in).
       -> test_activity_offer_is_null_when_no_seed_activity_matches
       -> test_activity_offer_returns_matching_seed_activity_and_records_engagement
       -> test_activity_offer_is_null_on_idempotent_replay
  MN-7 The mood set is per-age-band and config-driven, not hard-coded.
       -> test_mood_set_endpoint_reflects_age_band_config

FR-12-01 (closes DEF-005 — the internal caller/auth contract): a real submit scores the check-in
in-process and never fails the check-in response on a scoring error.
  -> test_submit_scores_checkin_and_creates_flag
  -> test_submit_clean_reflection_does_not_flag
  -> test_idempotent_replay_does_not_rescore
  -> test_scoring_failure_never_fails_checkin_response

FR-04-06 — POST /api/v1/check-ins/sync (offline check-in sync, client_entry_id idempotency key):
  S1/S2 A first-time sync creates the row, captured_offline=True, scored like a normal submit -> 201.
       -> test_sync_first_time_creates_offline_row
       -> test_sync_scores_like_a_normal_submit
  S3   A retried sync of the SAME client_entry_id never double-creates -> 200, same row.
       -> test_sync_retry_same_entry_id_is_idempotent_200
  MN   A DIFFERENT client_entry_id landing on the same day is still a hard 409 (one-per-day unchanged).
       -> test_sync_different_entry_id_same_day_is_409
  MN   Access window / consent / mood-band / reflection-required all still apply on a FIRST sync
       (evaluated at sync time, not capture time — ticket "do not weaken the access-window rule").
       -> test_sync_outside_access_window_is_403
       -> test_sync_invalid_mood_is_422
       -> test_sync_reflection_required_blocks_without_reflection
  MN   A request body's client_entry_id is the only new field vs the online submit shape.
       -> test_sync_body_shape
"""
from datetime import time

import pytest
from fastapi import HTTPException

from src.application.checkin import services as checkin_svc
from src.constants.enums import ParentalConsentStatus, StudentAgeBand
from src.domain.checkin import services as checkin_db
from src.domain.compliance import services as compliance_db
from src.domain.org.models import CalendarConfig
from src.schemas.checkin import CheckInSyncCreate

CHECKINS = "/api/v1/check-ins"
CHECKIN_CONFIG = "/api/v1/check-ins/config"
CHECKIN_SYNC = "/api/v1/check-ins/sync"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _open_all_day_window(db, school, timezone: str = "UTC") -> CalendarConfig:
    row = CalendarConfig(
        school_id=school.id, window_start=time(0, 0), window_end=time(23, 59, 59),
        timezone=timezone,
    )
    db.add(row)
    db.commit()
    return row


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
    """Wire the two prerequisite guards open (window + consent) and return a bearer token."""
    _open_all_day_window(db, school)
    _verify_consent(db, student)
    return _student_token(client, school, student)


# ---- S1 / S2 — happy path ----------------------------------------------------------------------


def test_submit_mood_only_success(client, db, make_school, make_student):
    school = make_school(code="CKS-1")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 201
    body = r.json()
    assert body["checkin_id"]
    assert body["activity_offer"] is None


def test_submit_with_optional_reflection_is_saved(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn

    school = make_school(code="CKS-2")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(
        CHECKINS, json={"mood_value": 4, "reflection_text": "Had a good day"}, headers=_auth(token)
    )
    assert r.status_code == 201
    row = db.get(CheckIn, r.json()["checkin_id"])
    assert row.reflection_text == "Had a good day"
    assert row.mood_value == 4
    assert row.within_window is True


# ---- S3 — school-required reflection ------------------------------------------------------------


def test_reflection_required_blocks_without_reflection(client, db, make_school, make_student):
    school = make_school(code="CKS-3")
    student = make_student(school)
    token = _ready(db, client, school, student)
    checkin_db.set_checkin_settings(db, school.id, require_reflection=True)
    db.commit()
    r = client.post(CHECKINS, json={"mood_value": 3}, headers=_auth(token))
    assert r.status_code == 422
    assert "reflection" in r.json()["detail"].lower()


def test_reflection_required_allows_with_reflection(client, db, make_school, make_student):
    school = make_school(code="CKS-4")
    student = make_student(school)
    token = _ready(db, client, school, student)
    checkin_db.set_checkin_settings(db, school.id, require_reflection=True)
    db.commit()
    r = client.post(
        CHECKINS, json={"mood_value": 3, "reflection_text": "ok"}, headers=_auth(token)
    )
    assert r.status_code == 201


def test_checkin_settings_upsert_updates_existing_row(db, make_school):
    school = make_school(code="CKS-4B")
    checkin_db.set_checkin_settings(db, school.id, require_reflection=True)
    db.commit()
    updated = checkin_db.set_checkin_settings(db, school.id, require_reflection=False)
    db.commit()
    assert updated.require_reflection is False
    assert checkin_db.get_checkin_settings(db, school.id).require_reflection is False


def test_reflection_required_rejects_whitespace_only(client, db, make_school, make_student):
    school = make_school(code="CKS-5")
    student = make_student(school)
    token = _ready(db, client, school, student)
    checkin_db.set_checkin_settings(db, school.id, require_reflection=True)
    db.commit()
    r = client.post(
        CHECKINS, json={"mood_value": 3, "reflection_text": "   "}, headers=_auth(token)
    )
    assert r.status_code == 422


# ---- MN-1 — access window (FR-07-03 guard wired first) -------------------------------------------


def test_outside_access_window_is_403_and_writes_nothing(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn

    school = make_school(code="CKS-6")
    student = make_student(school)
    _verify_consent(db, student)
    row = CalendarConfig(
        school_id=school.id, window_start=time(3, 0), window_end=time(3, 1), timezone="UTC"
    )
    db.add(row)
    db.commit()
    token = _student_token(client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 403
    assert db.query(CheckIn).count() == 0


def test_no_calendar_config_is_403(client, db, make_school, make_student):
    school = make_school(code="CKS-7")
    student = make_student(school)
    _verify_consent(db, student)
    token = _student_token(client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 403


# ---- MN-2 — consent-before-use (FR-20-06) --------------------------------------------------------


def test_unverified_consent_is_403(client, db, make_school, make_student):
    school = make_school(code="CKS-8")
    student = make_student(school)
    _open_all_day_window(db, school)
    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.pending
    )
    db.commit()
    token = _student_token(client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 403


def test_no_consent_record_at_all_is_403(client, db, make_school, make_student):
    school = make_school(code="CKS-9")
    student = make_student(school)
    _open_all_day_window(db, school)
    token = _student_token(client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 403


# ---- MN-3 — one-per-day + idempotency -------------------------------------------------------------


def test_duplicate_day_different_content_is_409(client, db, make_school, make_student):
    school = make_school(code="CKS-10")
    student = make_student(school)
    token = _ready(db, client, school, student)
    first = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert first.status_code == 201
    second = client.post(CHECKINS, json={"mood_value": 1}, headers=_auth(token))
    assert second.status_code == 409


def test_exact_retry_is_idempotent_201(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn

    school = make_school(code="CKS-11")
    student = make_student(school)
    token = _ready(db, client, school, student)
    body = {"mood_value": 4, "reflection_text": "same"}
    first = client.post(CHECKINS, json=body, headers=_auth(token))
    retry = client.post(CHECKINS, json=body, headers=_auth(token))
    assert first.status_code == 201
    assert retry.status_code == 201
    assert first.json()["checkin_id"] == retry.json()["checkin_id"]
    assert db.query(CheckIn).filter(CheckIn.student_id == student.id).count() == 1


# ---- MN-4 — mood must be in the caller's age-banded set ------------------------------------------


def test_mood_outside_bands_set_is_422(client, db, make_school, make_student):
    school = make_school(code="CKS-12")
    student = make_student(school, age_band=StudentAgeBand.b5_7)  # config default: {1,3,5}
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 0}, headers=_auth(token))
    assert r.status_code == 422


def test_mood_within_bands_set_succeeds(client, db, make_school, make_student):
    school = make_school(code="CKS-13")
    student = make_student(school, age_band=StudentAgeBand.b5_7)
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 3}, headers=_auth(token))
    assert r.status_code == 201


def test_mood_out_of_global_range_is_422(client, db, make_school, make_student):
    school = make_school(code="CKS-14")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 9}, headers=_auth(token))
    assert r.status_code == 422


# ---- MN-5 — self-only ------------------------------------------------------------------------------


def test_student_cannot_be_impersonated_no_id_param_exists():
    from src.schemas.checkin import CheckInCreate

    assert set(CheckInCreate.model_fields) == {"mood_value", "reflection_text"}


@pytest.mark.authz
def test_staff_session_denied_403(client, monkeypatch, make_school, make_staff):
    from src.constants.enums import StaffRole

    school = make_school(code="CKS-15")
    make_staff(school, role=StaffRole.teacher)
    r = client.post(
        "/api/v1/auth/staff/sign-in", json={"email": "t@oakwood.edu", "password": "Password123"}
    )
    token = r.json()["access_token"]
    resp = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert resp.status_code == 403


def test_no_auth_is_401():
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as c:
        r = c.post(CHECKINS, json={"mood_value": 4})
    assert r.status_code in (401, 403)  # HTTPBearer auto_error -> 403 with no header, 401 bad token


# ---- MN-6 — activity_offer (FR-05-01, closes DEF-010) ----------------------------------------------


def test_activity_offer_is_null_when_no_seed_activity_matches(client, db, make_school, make_student):
    """No seed activity exists at all in this test's DB -> a valid empty state, not an error."""
    school = make_school(code="CKS-16")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.json()["activity_offer"] is None


def test_activity_offer_returns_matching_seed_activity_and_records_engagement(
    client, db, make_school, make_student
):
    from sqlalchemy import select

    from src.constants.enums import ActivityAgeBand, ActivityEngagementStatus, ActivityType
    from src.domain.checkin import services as checkin_db_
    from src.domain.checkin.models import ActivityEngagement

    school = make_school(code="CKS-16B")
    student = make_student(school, age_band=StudentAgeBand.b8_11)
    token = _ready(db, client, school, student)
    activity = checkin_db_.add_seed_activity(
        db, title="Breathing break", type=ActivityType.breathing,
        age_band=ActivityAgeBand.all, topic=None,
    )
    db.commit()

    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 201
    offer = r.json()["activity_offer"]
    assert offer is not None
    assert offer["activity_id"] == str(activity.id)
    assert offer["title"] == "Breathing break"
    assert offer["type"] == "breathing"

    checkin_id = r.json()["checkin_id"]
    engagement = db.scalar(
        select(ActivityEngagement).where(ActivityEngagement.checkin_id == checkin_id)
    )
    assert engagement.student_id == student.id
    assert engagement.activity_id == activity.id
    assert engagement.status == ActivityEngagementStatus.offered


def test_activity_offer_is_null_on_idempotent_replay(client, db, make_school, make_student):
    from src.constants.enums import ActivityAgeBand, ActivityType
    from src.domain.checkin import services as checkin_db_

    school = make_school(code="CKS-16C")
    student = make_student(school)
    token = _ready(db, client, school, student)
    checkin_db_.add_seed_activity(
        db, title="Grounding", type=ActivityType.grounding, age_band=ActivityAgeBand.all, topic=None
    )
    db.commit()

    first = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert first.status_code == 201
    assert first.json()["activity_offer"] is not None

    replay = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert replay.status_code == 201
    assert replay.json()["checkin_id"] == first.json()["checkin_id"]
    assert replay.json()["activity_offer"] is None


# ---- MN-7 — config-driven mood set ------------------------------------------------------------------


def test_checkin_config_endpoint_reflects_age_band_config(client, db, make_school, make_student):
    school = make_school(code="CKS-17")
    young = make_student(school, name="Young", age_band=StudentAgeBand.b5_7)
    older = make_student(school, name="Older", age_band=StudentAgeBand.b12_18)
    _open_all_day_window(db, school)
    _verify_consent(db, young)
    _verify_consent(db, older)
    young_token = _student_token(client, school, young)
    older_token = _student_token(client, school, older)
    assert client.get(CHECKIN_CONFIG, headers=_auth(young_token)).json()["mood_set"] == [1, 3, 5]
    assert client.get(CHECKIN_CONFIG, headers=_auth(older_token)).json()["mood_set"] == [
        0, 1, 2, 3, 4, 5,
    ]


# ---- FR-04-03 — GET /check-ins/config: age-band -> mode/read_aloud -------------------------------


@pytest.mark.parametrize(
    "age_band,expected_mode,expected_read_aloud",
    [
        (StudentAgeBand.b5_7, "simple", True),
        (StudentAgeBand.b8_11, "simple", True),
        (StudentAgeBand.b12_18, "rich", False),
    ],
)
def test_checkin_config_mode_and_read_aloud_by_age_band(
    client, db, make_school, make_student, age_band, expected_mode, expected_read_aloud
):
    school = make_school(code=f"CKS-{age_band.value}")
    student = make_student(school, age_band=age_band)
    _open_all_day_window(db, school)
    _verify_consent(db, student)
    token = _student_token(client, school, student)
    r = client.get(CHECKIN_CONFIG, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == expected_mode
    assert body["read_aloud"] is expected_read_aloud


def test_checkin_config_requires_auth(client):
    # No Authorization header at all -> the security dependency itself rejects (403 "Not
    # authenticated"), the same convention every other protected endpoint in this codebase uses;
    # a present-but-invalid token is what yields the middleware's own 401 "Invalid token".
    assert client.get(CHECKIN_CONFIG).status_code == 403


def test_mood_set_for_band_is_config_driven_not_hardcoded(monkeypatch):
    from config.env_config import settings

    monkeypatch.setattr(settings, "mood_set_b5_7", "3")
    assert checkin_svc.mood_set_for_band(StudentAgeBand.b5_7) == [3]


# ---- service-layer direct coverage (guard-order + 500 defensive branch) --------------------------


# ---- FR-12-01 — scoring wired into the real submit path (closes DEF-005) -------------------------


def test_submit_scores_checkin_and_creates_flag(client, db, make_school, make_student):
    from src.domain.risk.models import Flag

    school = make_school(code="CKS-19")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(
        CHECKINS, json={"mood_value": 3, "reflection_text": "i feel unsafe"}, headers=_auth(token)
    )
    assert r.status_code == 201
    checkin_id = r.json()["checkin_id"]
    flag = db.query(Flag).filter(Flag.checkin_id == checkin_id).first()
    assert flag is not None and float(flag.risk_score) == 0.90 and flag.band is None


def test_submit_clean_reflection_does_not_flag(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn
    from src.domain.risk.models import Flag

    school = make_school(code="CKS-20")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(
        CHECKINS, json={"mood_value": 4, "reflection_text": "great day"}, headers=_auth(token)
    )
    assert r.status_code == 201
    row = db.get(CheckIn, r.json()["checkin_id"])
    assert row.scored is True
    assert db.query(Flag).filter(Flag.checkin_id == row.id).count() == 0


def test_idempotent_replay_does_not_rescore(client, db, make_school, make_student):
    from src.domain.risk.models import Flag

    school = make_school(code="CKS-21")
    student = make_student(school)
    token = _ready(db, client, school, student)
    body = {"mood_value": 3, "reflection_text": "i feel hopeless"}
    first = client.post(CHECKINS, json=body, headers=_auth(token))
    retry = client.post(CHECKINS, json=body, headers=_auth(token))
    assert first.status_code == 201 and retry.status_code == 201
    checkin_id = first.json()["checkin_id"]
    assert db.query(Flag).filter(Flag.checkin_id == checkin_id).count() == 1  # no duplicate flag


def test_scoring_failure_never_fails_checkin_response(
    client, db, make_school, make_student, monkeypatch
):
    from src.domain.checkin.models import CheckIn
    from src.routers import checkins as checkins_router

    school = make_school(code="CKS-22")
    student = make_student(school)
    token = _ready(db, client, school, student)

    def boom(_db, _checkin):
        raise RuntimeError("scoring exploded")

    monkeypatch.setattr(checkins_router.risk_svc, "score_checkin", boom)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 201  # scoring failure never surfaces as a failed check-in
    row = db.get(CheckIn, r.json()["checkin_id"])
    assert row.scored is False  # left queued for the process_pending worker's retry/dead-letter


# ---- FR-04-06 — POST /check-ins/sync (offline sync, client_entry_id idempotency) -----------------


def test_sync_body_shape():
    assert set(CheckInSyncCreate.model_fields) == {
        "client_entry_id", "mood_value", "reflection_text",
    }


def test_sync_first_time_creates_offline_row(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn

    school = make_school(code="SYN-1")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(
        CHECKIN_SYNC,
        json={"client_entry_id": "entry-1", "mood_value": 4, "reflection_text": "offline day"},
        headers=_auth(token),
    )
    assert r.status_code == 201
    row = db.get(CheckIn, r.json()["checkin_id"])
    assert row.captured_offline is True
    assert row.client_entry_id == "entry-1"
    assert row.mood_value == 4
    assert row.reflection_text == "offline day"


def test_sync_scores_like_a_normal_submit(client, db, make_school, make_student):
    from src.domain.risk.models import Flag

    school = make_school(code="SYN-2")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(
        CHECKIN_SYNC,
        json={"client_entry_id": "entry-2", "mood_value": 3, "reflection_text": "i feel unsafe"},
        headers=_auth(token),
    )
    assert r.status_code == 201
    checkin_id = r.json()["checkin_id"]
    flag = db.query(Flag).filter(Flag.checkin_id == checkin_id).first()
    assert flag is not None and float(flag.risk_score) == 0.90


def test_sync_retry_same_entry_id_is_idempotent_200(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn

    school = make_school(code="SYN-3")
    student = make_student(school)
    token = _ready(db, client, school, student)
    body = {"client_entry_id": "entry-3", "mood_value": 4, "reflection_text": "same"}
    first = client.post(CHECKIN_SYNC, json=body, headers=_auth(token))
    retry = client.post(CHECKIN_SYNC, json=body, headers=_auth(token))
    assert first.status_code == 201
    assert retry.status_code == 200
    assert first.json()["checkin_id"] == retry.json()["checkin_id"]
    assert db.query(CheckIn).filter(CheckIn.student_id == student.id).count() == 1


def test_sync_different_entry_id_same_day_is_409(client, db, make_school, make_student):
    school = make_school(code="SYN-4")
    student = make_student(school)
    token = _ready(db, client, school, student)
    first = client.post(
        CHECKIN_SYNC,
        json={"client_entry_id": "entry-4a", "mood_value": 4},
        headers=_auth(token),
    )
    assert first.status_code == 201
    second = client.post(
        CHECKIN_SYNC,
        json={"client_entry_id": "entry-4b", "mood_value": 1},
        headers=_auth(token),
    )
    assert second.status_code == 409


def test_sync_outside_access_window_is_403(client, db, make_school, make_student):
    from src.domain.checkin.models import CheckIn

    school = make_school(code="SYN-5")
    student = make_student(school)
    _verify_consent(db, student)
    row = CalendarConfig(
        school_id=school.id, window_start=time(3, 0), window_end=time(3, 1), timezone="UTC"
    )
    db.add(row)
    db.commit()
    token = _student_token(client, school, student)
    r = client.post(
        CHECKIN_SYNC, json={"client_entry_id": "entry-5", "mood_value": 4}, headers=_auth(token)
    )
    assert r.status_code == 403
    assert db.query(CheckIn).count() == 0


def test_sync_invalid_mood_is_422(client, db, make_school, make_student):
    school = make_school(code="SYN-6")
    student = make_student(school, age_band=StudentAgeBand.b5_7)
    token = _ready(db, client, school, student)
    r = client.post(
        CHECKIN_SYNC, json={"client_entry_id": "entry-6", "mood_value": 0}, headers=_auth(token)
    )
    assert r.status_code == 422


def test_sync_reflection_required_blocks_without_reflection(client, db, make_school, make_student):
    school = make_school(code="SYN-7")
    student = make_student(school)
    token = _ready(db, client, school, student)
    checkin_db.set_checkin_settings(db, school.id, require_reflection=True)
    db.commit()
    r = client.post(
        CHECKIN_SYNC, json={"client_entry_id": "entry-7", "mood_value": 3}, headers=_auth(token)
    )
    assert r.status_code == 422


def test_submit_checkin_guard_order_window_before_consent(db, make_school, make_student):
    """Access-window guard raises BEFORE the consent guard is ever reached (no consent row exists
    at all here — if consent ran first this would also be 403, so this proves ORDER via a distinct
    absence: the window guard's own detail message)."""
    school = make_school(code="CKS-18")
    student = make_student(school)
    with pytest.raises(HTTPException) as exc:
        checkin_svc.submit_checkin(db, student, 4, None)
    assert exc.value.status_code == 403
    assert exc.value.detail == "check-in is not open right now"
