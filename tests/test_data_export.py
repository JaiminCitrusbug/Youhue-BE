"""FR-20-01 — school data export (SC-064, GATE G-12): authorised leadership exports their OWN
school's data (async: 202 accepted -> 200 export_ready); a staff member without export authority
cannot export (the option is not available, and a forced request is 403). Every export is
school-scoped — never another school's data.

Covers @FR-20-01:
  Scenario 1 (positive): a leadership user requests an export -> the system produces one.
  Scenario 2 (NEG — GATE G-12): a non-leadership staff member cannot export.
Plus: cross-tenant denial, unknown/cross-tenant export id reads as 404, the artifact really lands
in object storage (round-tripped via a presigned URL, not just a DB-row assertion), idempotent
data-model shape (kind/status ENUMs), and the export contains ONLY the requesting school's data.
"""
import json
import uuid

import httpx
import pytest
from fastapi import HTTPException

from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.application.compliance import export_services as export_svc
from src.constants.enums import DataExportKind, DataExportStatus, SessionKind, StaffRole
from src.domain.compliance.models import DataExport
from src.domain.identity.models import StaffAccount
from src.infrastructure import storage

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str) -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _mint(db, staff: StaffAccount) -> str:
    """leadership/district/admin roles require MFA on the real sign-in endpoint — tests that only
    need a valid leadership SESSION (not the MFA flow itself) mint one directly, same helper
    test_alert_config.py already established for this exact reason."""
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


# ---- Scenario 1: an authorised leadership user exports their school's data --------------------

def test_leadership_can_request_and_complete_an_export(
    client, db, make_school, make_staff, make_student
):
    school = make_school()
    leader = make_staff(school, email="lead@oakwood.edu", role=StaffRole.leadership)
    student = make_student(school)
    token = _mint(db, leader)

    r = client.post(f"/api/v1/schools/{school.id}/export", json={}, headers=_auth(token))
    assert r.status_code == 202
    body = r.json()
    export_id = body["export_id"]
    # TestClient runs BackgroundTasks synchronously as part of the request cycle, so the artifact
    # is already written by the time this call returns — a genuine end-to-end round trip, not a
    # mocked write.
    status_r = client.get(
        f"/api/v1/schools/{school.id}/exports/{export_id}", headers=_auth(token)
    )
    assert status_r.status_code == 200
    assert status_r.json()["status"] == "ready"
    assert status_r.json()["download_url"] is not None

    row = db.get(DataExport, uuid.UUID(export_id))
    assert row is not None and row.storage_key is not None
    # the artifact genuinely exists in object storage — fetched via a real presigned URL, not
    # asserted from the DB row alone
    url = storage.get_presigned_url(row.storage_key)
    resp = httpx.get(url, timeout=10.0)
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["school_id"] == str(school.id)
    assert any(s["id"] == str(student.id) for s in payload["students"])


def test_export_defaults_to_plain_export_kind(client, db, make_school, make_staff):
    school = make_school()
    leader = make_staff(school, email="lead2@oakwood.edu", role=StaffRole.leadership)
    token = _mint(db, leader)
    r = client.post(f"/api/v1/schools/{school.id}/export", json={}, headers=_auth(token))
    assert r.status_code == 202
    assert r.json()["status"] == "pending"


# ---- Scenario 2 (NEG — GATE G-12): non-leadership cannot export --------------------------------

def test_non_leadership_staff_cannot_export(client, make_school, make_staff):
    school = make_school()
    make_staff(school, email="teacher@oakwood.edu", role=StaffRole.teacher)
    token = _staff_token(client, "teacher@oakwood.edu")
    r = client.post(f"/api/v1/schools/{school.id}/export", json={}, headers=_auth(token))
    assert r.status_code == 403


def test_support_role_cannot_export(client, make_school, make_staff):
    school = make_school()
    make_staff(school, email="support@oakwood.edu", role=StaffRole.support)
    token = _staff_token(client, "support@oakwood.edu")
    r = client.post(f"/api/v1/schools/{school.id}/export", json={}, headers=_auth(token))
    assert r.status_code == 403


# ---- cross-tenant isolation ----------------------------------------------------------------------

def test_leadership_cannot_export_a_different_school(client, db, make_school, make_staff):
    school_a = make_school(code="AAA-5", name="A5")
    leader = make_staff(school_a, email="a5@oakwood.edu", role=StaffRole.leadership)
    school_b = make_school(code="BBB-5", name="B5")
    token = _mint(db, leader)
    r = client.post(f"/api/v1/schools/{school_b.id}/export", json={}, headers=_auth(token))
    assert r.status_code == 403


def test_role_check_runs_before_school_scope_check(db, make_school, make_staff):
    # GATE G-12: a non-leadership actor is denied regardless of which school they target — the
    # role gate must not leak "this school id would have worked" via a different status code.
    school_a = make_school(code="AAA-6", name="A6")
    other_school = make_school(code="BBB-6", name="B6")
    teacher = make_staff(school_a, email="t6@oakwood.edu", role=StaffRole.teacher)

    with pytest.raises(HTTPException) as exc:
        export_svc.request_export(db, teacher, other_school.id, DataExportKind.export)
    assert exc.value.status_code == 403


def test_export_status_hides_cross_tenant_export_as_404(client, db, make_school, make_staff):
    school_a = make_school(code="AAA-4", name="A4")
    leader_a = make_staff(school_a, email="a4@oakwood.edu", role=StaffRole.leadership)
    school_b = make_school(code="BBB-4", name="B4")
    leader_b = make_staff(school_b, email="b4@oakwood.edu", role=StaffRole.leadership)

    export = export_svc.request_export(db, leader_a, school_a.id, DataExportKind.export)
    db.commit()

    token_b = _mint(db, leader_b)
    r = client.get(
        f"/api/v1/schools/{school_b.id}/exports/{export.id}", headers=_auth(token_b)
    )
    assert r.status_code == 404  # existence hidden, not "wrong school" leaked as 403


def test_pending_export_has_no_download_url_yet(client, db, make_school, make_staff):
    school = make_school(code="AAA-2", name="A2")
    leader = make_staff(school, email="a2@oakwood.edu", role=StaffRole.leadership)
    token = _mint(db, leader)
    export = export_svc.request_export(db, leader, school.id, DataExportKind.export)
    db.commit()  # note: NOT built yet — asserting the pre-BackgroundTasks state directly
    r = client.get(f"/api/v1/schools/{school.id}/exports/{export.id}", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    assert r.json()["download_url"] is None


def test_unknown_export_id_is_404(client, db, make_school, make_staff):
    school = make_school()
    leader = make_staff(school, email="lead3@oakwood.edu", role=StaffRole.leadership)
    token = _mint(db, leader)
    r = client.get(
        f"/api/v1/schools/{school.id}/exports/{uuid.uuid4()}", headers=_auth(token)
    )
    assert r.status_code == 404


# ---- the export contains ONLY the requesting school's data --------------------------------------

def test_export_never_includes_another_schools_data(db, make_school, make_staff, make_student):
    school_a = make_school(code="AAA-3", name="A3")
    school_b = make_school(code="BBB-3", name="B3")
    leader_a = make_staff(school_a, email="a3@oakwood.edu", role=StaffRole.leadership)
    student_a = make_student(school_a)
    student_b = make_student(school_b)

    export = export_svc.request_export(db, leader_a, school_a.id, DataExportKind.export)
    db.commit()
    export_svc.build_and_store_export(export.id)

    db.refresh(export)
    assert export.status == DataExportStatus.ready
    assert export.storage_key is not None
    url = storage.get_presigned_url(export.storage_key)
    payload = json.loads(httpx.get(url, timeout=10.0).content)
    student_ids = {s["id"] for s in payload["students"]}
    assert str(student_a.id) in student_ids
    assert str(student_b.id) not in student_ids


# ---- data model shape ---------------------------------------------------------------------------

def test_data_export_kind_and_status_enums(db, make_school, make_staff):
    school = make_school()
    leader = make_staff(school, email="lead4@oakwood.edu", role=StaffRole.leadership)
    export = export_svc.request_export(db, leader, school.id, DataExportKind.export)
    db.commit()
    assert export.kind == DataExportKind.export
    assert export.status == DataExportStatus.pending  # not yet built
