"""FR-20-06 — verifiable parental consent (COPPA): capture endpoint, the reusable consent-before-use
guard, and the no-sale/no-ads/minimum-data posture tripwires."""
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

import src.application.auth.staff as staff_mod
from src.application.compliance import services as compliance
from src.constants.enums import ParentalConsentStatus, StaffRole
from src.domain.compliance.models import AuditLog, ParentalConsent
from src.schemas.compliance import ConsentIn, ConsentOut
from src.schemas.students import StudentOut

SIGNIN = "/api/v1/auth/staff/sign-in"
ROOT = Path(__file__).resolve().parents[1]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, monkeypatch, email: str, pw: str = "Password123") -> str:
    """Signs in and, when the role forces MFA (`leadership` is always in `mfa_required_roles` —
    consent capture is a leadership-only action), completes the email-OTP challenge too, mirroring
    `test_auth_staff.py::test_mfa_challenge_then_verify`."""
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    signin = client.post(SIGNIN, json={"email": email, "password": pw})
    body = signin.json()
    if not body.get("mfa_required"):
        return body["access_token"]
    pending_token = body["access_token"]
    code = captured["body"].split()[-1]
    verify = client.post(
        "/api/v1/auth/staff/mfa/verify", json={"session_token": pending_token, "code": code}
    )
    return verify.json()["access_token"]


def _url(student_id) -> str:
    return f"/api/v1/students/{student_id}/consent"


# ---- POST /api/v1/students/{id}/consent (SC-088, leadership-only, school-mediated) -----------

def test_capture_consent_verified_returns_200(client, monkeypatch, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school, role=StaffRole.leadership)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    r = client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"status": "verified"}


def test_capture_consent_pending_returns_200(client, monkeypatch, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school, role=StaffRole.leadership)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    r = client.post(_url(student.id), json={"status": "pending"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}


def test_capture_consent_invalid_status_is_422(client, monkeypatch, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school, role=StaffRole.leadership)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    r = client.post(_url(student.id), json={"status": "definitely-approved"}, headers=_auth(token))
    assert r.status_code == 422


def test_capture_consent_persists_one_row_with_captured_at(
    client, monkeypatch, db, make_school, make_staff, make_student
):
    school = make_school()
    make_staff(school, role=StaffRole.leadership)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    row = db.query(ParentalConsent).filter(ParentalConsent.student_id == student.id).one()
    assert row.status == ParentalConsentStatus.verified
    assert row.captured_at is not None


def test_capture_consent_is_idempotent_one_row_per_student(
    client, monkeypatch, db, make_school, make_staff, make_student
):
    # Second capture for the same student UPDATES the one row (unique student_id, FR-20-06 migration
    # f20606a1b2c3) rather than inserting a duplicate.
    school = make_school()
    make_staff(school, role=StaffRole.leadership)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    client.post(_url(student.id), json={"status": "pending"}, headers=_auth(token))
    client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    rows = db.query(ParentalConsent).filter(ParentalConsent.student_id == student.id).all()
    assert len(rows) == 1
    assert rows[0].status == ParentalConsentStatus.verified


@pytest.mark.authz
def test_capture_consent_denies_non_leadership_role(
    client, monkeypatch, make_school, make_staff, make_student
):
    school = make_school()
    make_staff(school, role=StaffRole.teacher)  # only `leadership` records consent (SC-088)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    r = client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.authz
def test_capture_consent_denies_cross_school(client, monkeypatch, make_school, make_staff, make_student):
    school_a = make_school(code="AAA-1", name="A")
    make_staff(school_a, email="a@oakwood.edu", role=StaffRole.leadership)
    school_b = make_school(code="BBB-2", name="B")
    student_b = make_student(school_b)
    token = _staff_token(client, monkeypatch, "a@oakwood.edu")
    r = client.post(_url(student_b.id), json={"status": "verified"}, headers=_auth(token))
    assert r.status_code == 403


@pytest.mark.authz
def test_capture_consent_denies_student_session(client, make_school, make_student):
    school = make_school(code="OAK-9")
    student = make_student(school)
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": "OAK-9", "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    assert r.status_code == 403


def test_capture_consent_writes_immutable_audit(
    client, monkeypatch, db, make_school, make_staff, make_student
):
    school = make_school()
    make_staff(school, role=StaffRole.leadership)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    rows = db.query(AuditLog).all()
    assert any(r.action == "consent.capture" and r.target == str(student.id) for r in rows)


def test_denied_capture_is_audited(client, monkeypatch, db, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school, role=StaffRole.teacher)
    student = make_student(school)
    token = _staff_token(client, monkeypatch, "t@oakwood.edu")
    client.post(_url(student.id), json={"status": "verified"}, headers=_auth(token))
    rows = db.query(AuditLog).all()
    assert any(r.action == "consent.capture.denied" for r in rows)  # intrusion signal, per INFRA-02


# ---- get_consent_status + the reusable consent-before-use guard (ticket §NEG) -------------------
# NOT wired to any production data-use path yet (FR-04-01, the "associated data use", does not exist
# in this codebase yet) — these tests prove the guard function itself works, per the ticket's ask.

def test_get_consent_status_defaults_to_pending_when_no_record(db):
    assert compliance.get_consent_status(db, uuid.uuid4()) == ParentalConsentStatus.pending


def test_get_consent_status_reflects_captured_value(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    from src.domain.compliance import services as compliance_db

    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.verified
    )
    db.commit()
    assert compliance.get_consent_status(db, student.id) == ParentalConsentStatus.verified


def test_require_verified_consent_blocks_on_pending(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    from src.domain.compliance import services as compliance_db

    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.pending
    )
    db.commit()
    with pytest.raises(HTTPException) as exc:
        compliance.require_verified_consent(db, student.id)
    assert exc.value.status_code == 403


def test_require_verified_consent_blocks_when_no_record_at_all(db):
    # Consent-before-use gate (ticket §NEG): absence of a record must never read as consent.
    with pytest.raises(HTTPException) as exc:
        compliance.require_verified_consent(db, uuid.uuid4())
    assert exc.value.status_code == 403


def test_require_verified_consent_allows_on_verified(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    from src.domain.compliance import services as compliance_db

    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.verified
    )
    db.commit()
    compliance.require_verified_consent(db, student.id)  # does not raise


# ---- no data-sale / no ads-to-children / minimum-data posture (ticket §Must-nots, NEG) ----------

_BANNED_DEP_SUBSTRINGS = (
    "google-ads", "google_ads", "googleads", "facebook-business", "facebook-ads",
    "segment-analytics", "mixpanel", "amplitude", "doubleclick", "adroll", "admob",
    "criteo", "branch-sdk", "appsflyer", "fullstory", "hotjar", "braze", "adjust-sdk",
    "data-broker", "ad-exchange",
)


def test_no_ad_sdk_or_data_broker_dependency_declared():
    """Regression tripwire (ticket §Must-nots: no ad SDK, no third-party data-sharing/ad-sale
    integration). Scans every dependency manifest this repo actually installs from."""
    manifests = [
        ROOT / "requirements.txt",
        ROOT / "requirements-dev.txt",
        ROOT / "pyproject.toml",
    ]
    text = "\n".join(p.read_text(encoding="utf-8").lower() for p in manifests if p.exists())
    hits = [dep for dep in _BANNED_DEP_SUBSTRINGS if dep in text]
    assert not hits, f"ad/data-broker dependency found: {hits}"


def test_consent_schemas_carry_no_surplus_fields():
    """Minimum-data posture: the consent request/response carry ONLY `status` — no parent PII
    (name/email/phone), no free-text, nothing beyond what COPPA verifiable-consent capture needs."""
    assert set(ConsentIn.model_fields) == {"status"}
    assert set(ConsentOut.model_fields) == {"status"}


def test_student_schema_carries_no_surplus_child_data_fields():
    """The student read schema exposes only what staff need to identify/manage a roster row —
    no address, phone, DOB, SSN, or other surplus child-data field."""
    assert set(StudentOut.model_fields) == {"id", "display_name", "age_band", "school_id"}
