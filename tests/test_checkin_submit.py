"""FR-04-01 — POST /api/v1/check-ins (mood + optional/required reflection) + GET /mood-set.

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
  MN-6 activity_offer is always null (FR-05-01 stub field, DEF-010).
       -> test_activity_offer_is_always_null
  MN-7 The mood set is per-age-band and config-driven, not hard-coded.
       -> test_mood_set_endpoint_reflects_age_band_config

FR-12-01 (closes DEF-005 — the internal caller/auth contract): a real submit scores the check-in
in-process and never fails the check-in response on a scoring error.
  -> test_submit_scores_checkin_and_creates_flag
  -> test_submit_clean_reflection_does_not_flag
  -> test_idempotent_replay_does_not_rescore
  -> test_scoring_failure_never_fails_checkin_response
"""
from datetime import time

import pytest
from fastapi import HTTPException

from src.application.checkin import services as checkin_svc
from src.constants.enums import ParentalConsentStatus, StudentAgeBand
from src.domain.checkin import services as checkin_db
from src.domain.compliance import services as compliance_db
from src.domain.org.models import CalendarConfig

CHECKINS = "/api/v1/check-ins"
MOOD_SET = "/api/v1/check-ins/mood-set"


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


# ---- MN-6 — activity_offer stub -------------------------------------------------------------------


def test_activity_offer_is_always_null(client, db, make_school, make_student):
    school = make_school(code="CKS-16")
    student = make_student(school)
    token = _ready(db, client, school, student)
    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.json()["activity_offer"] is None


# ---- MN-7 — config-driven mood set ------------------------------------------------------------------


def test_mood_set_endpoint_reflects_age_band_config(client, db, make_school, make_student):
    school = make_school(code="CKS-17")
    young = make_student(school, name="Young", age_band=StudentAgeBand.b5_7)
    older = make_student(school, name="Older", age_band=StudentAgeBand.b12_18)
    _open_all_day_window(db, school)
    _verify_consent(db, young)
    _verify_consent(db, older)
    young_token = _student_token(client, school, young)
    older_token = _student_token(client, school, older)
    assert client.get(MOOD_SET, headers=_auth(young_token)).json()["values"] == [1, 3, 5]
    assert client.get(MOOD_SET, headers=_auth(older_token)).json()["values"] == [0, 1, 2, 3, 4, 5]


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
