"""Admin maintains the platform seed activity set (FR-19-04).

Covers @FR-19-04:
  Scenario 1 — the internal team add/edit/removes an activity and the change applies to the seed set
               available to all schools.
  Scenario 2 — the maintained seed set is available to all schools (scope=seed, school_id=NULL —
               global, so a teacher at any school reads the same set; the teacher-facing read is
               FR-14-02 and only the storage/CRUD is asserted here).
Maintenance is gated by admin RBAC: a role without `manage_seed` gets 403 (audit-logged); errors
(unknown id -> 404, invalid enum -> 422) are surfaced, never silently dropped.
"""
import src.application.auth.admin as admin_mod
from src.constants.enums import ActivityScope
from src.domain.checkin.models import Activity
from src.domain.compliance.models import AuditLog

BASE = "/api/v1/admin/seed-activities"
SIGNIN = "/api/v1/admin/sign-in"


def _auth(client, monkeypatch, make_admin, *, email="admin@youhue.app", role=None):
    """Create an admin, complete the two-phase (password + email-OTP) sign-in, return a bearer hdr."""
    from src.constants.enums import AdminRole

    make_admin(email=email, password="Password123", role=role or AdminRole.superadmin)
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        admin_mod, "send_email", lambda to, subject, body: captured.update(body=body)
    )
    client.post(SIGNIN, json={"email": email, "password": "Password123"})
    code = captured["body"].split()[-1]
    token = client.post(
        SIGNIN, json={"email": email, "password": "Password123", "mfa_code": code}
    ).json()["admin_session"]
    return {"Authorization": f"Bearer {token}"}


def _payload(**over):
    body = {"title": "Box breathing", "type": "breathing", "age_band": "all", "topic": "Calming"}
    body.update(over)
    return body


# ---- Scenario 1: create / edit / retire the seed set ----

def test_create_seed_activity_available_to_all_schools(client, db, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    r = client.post(BASE, json=_payload(), headers=hdr)
    assert r.status_code == 200
    activity = r.json()["activity"]
    assert activity["title"] == "Box breathing"
    assert activity["type"] == "breathing"
    assert activity["active"] is True
    # persisted as a GLOBAL seed row (scope=seed, school_id NULL) -> available to every school
    row = db.query(Activity).filter(Activity.id == activity["id"]).one()
    assert row.scope == ActivityScope.seed
    assert row.school_id is None
    # maintenance is audit-logged
    assert db.query(AuditLog).filter(AuditLog.action == "admin.seed.created").count() == 1


def test_created_activity_shows_in_the_set(client, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    client.post(BASE, json=_payload(title="Worry jar", type="grounding"), headers=hdr)
    client.post(BASE, json=_payload(title="Box breathing"), headers=hdr)
    listed = client.get(BASE, headers=hdr)
    assert listed.status_code == 200
    titles = [a["title"] for a in listed.json()["activities"]]
    assert titles == ["Box breathing", "Worry jar"]  # active, ordered by title


def test_edit_seed_activity(client, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    aid = client.post(BASE, json=_payload(), headers=hdr).json()["activity"]["id"]
    r = client.patch(f"{BASE}/{aid}", json={"title": "Square breathing", "topic": "Focus"}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["activity"]["title"] == "Square breathing"
    assert r.json()["activity"]["topic"] == "Focus"
    assert r.json()["activity"]["type"] == "breathing"  # untouched field unchanged


def test_retire_removes_from_the_set(client, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    aid = client.post(BASE, json=_payload(), headers=hdr).json()["activity"]["id"]
    r = client.delete(f"{BASE}/{aid}", headers=hdr)
    assert r.status_code == 200
    assert r.json()["activity"]["active"] is False
    # retired -> gone from the default (schools-consume) set, still visible with include_retired
    assert client.get(BASE, headers=hdr).json()["activities"] == []
    all_rows = client.get(f"{BASE}?include_retired=true", headers=hdr).json()["activities"]
    assert len(all_rows) == 1 and all_rows[0]["active"] is False


def test_reinstate_via_edit(client, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    aid = client.post(BASE, json=_payload(), headers=hdr).json()["activity"]["id"]
    client.delete(f"{BASE}/{aid}", headers=hdr)
    r = client.patch(f"{BASE}/{aid}", json={"active": True}, headers=hdr)
    assert r.status_code == 200 and r.json()["activity"]["active"] is True
    assert len(client.get(BASE, headers=hdr).json()["activities"]) == 1


# ---- RBAC: a role without `manage_seed` is denied (403 + audit) on every verb ----

def test_support_role_denied_create_and_audit_logged(client, db, monkeypatch, make_admin):
    from src.constants.enums import AdminRole

    hdr = _auth(client, monkeypatch, make_admin, email="support@youhue.app", role=AdminRole.support)
    r = client.post(BASE, json=_payload(), headers=hdr)
    assert r.status_code == 403
    assert db.query(Activity).count() == 0  # nothing written
    denials = db.query(AuditLog).filter(AuditLog.action == "admin.rbac.denied:manage_seed").all()
    assert len(denials) == 1 and denials[0].school_id is None


def test_support_role_denied_list_edit_retire(client, monkeypatch, make_admin):
    from src.constants.enums import AdminRole

    # seed a row as superadmin first, then act as the limited support role
    su = _auth(client, monkeypatch, make_admin)
    aid = client.post(BASE, json=_payload(), headers=su).json()["activity"]["id"]
    sup = _auth(client, monkeypatch, make_admin, email="support@youhue.app", role=AdminRole.support)
    assert client.get(BASE, headers=sup).status_code == 403
    assert client.patch(f"{BASE}/{aid}", json={"title": "x"}, headers=sup).status_code == 403
    assert client.delete(f"{BASE}/{aid}", headers=sup).status_code == 403


def test_staff_session_cannot_reach_seed_maintenance(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    token = client.post(
        "/api/v1/auth/staff/sign-in", json={"email": "t@oakwood.edu", "password": "Password123"}
    ).json()["access_token"]
    r = client.post(BASE, json=_payload(), headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403  # a staff session never resolves an admin route


def test_unauthenticated_denied(client):
    assert client.get(BASE).status_code == 403
    assert client.post(BASE, json=_payload()).status_code == 403


# ---- errors surfaced: 404 unknown id, 422 invalid enum ----

def test_edit_unknown_id_404(client, monkeypatch, make_admin):
    import uuid

    hdr = _auth(client, monkeypatch, make_admin)
    r = client.patch(f"{BASE}/{uuid.uuid4()}", json={"title": "x"}, headers=hdr)
    assert r.status_code == 404


def test_retire_unknown_id_404(client, monkeypatch, make_admin):
    import uuid

    hdr = _auth(client, monkeypatch, make_admin)
    assert client.delete(f"{BASE}/{uuid.uuid4()}", headers=hdr).status_code == 404


def test_invalid_type_rejected_422(client, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    r = client.post(BASE, json=_payload(type="not_a_real_type"), headers=hdr)
    assert r.status_code == 422


def test_blank_title_rejected_422(client, monkeypatch, make_admin):
    hdr = _auth(client, monkeypatch, make_admin)
    assert client.post(BASE, json=_payload(title=""), headers=hdr).status_code == 422
