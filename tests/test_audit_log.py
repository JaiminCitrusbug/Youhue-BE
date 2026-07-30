"""FR-20-05 — immutable audit trail + admin audit-log viewer (SC-080).

Covers @FR-20-05:
  Scenario 1 (positive): an authorised internal admin opens the audit-log viewer, applies a
    filter, and sees the recorded events (when/actor/action), including a support-access entry.
  Scenario 2 (positive): a support access to a school's children's data (FR-19-02) writes an
    immutable audit entry with the actor + target school — queryable through this viewer.
  Scenario 3 (NEG): the log cannot be edited or deleted by anyone, including an admin — enforced by
    the DB-level append-only trigger (migration c0ffee000001; also covered directly by
    `tests/test_isolation.py::test_audit_log_is_immutable`, INFRA-02/FR-20-07's own choke point).
  Scenario 4 (NEG): the viewer is read + filter + export only — no PATCH/PUT/DELETE route exists
    on `/admin/audit-log` at all (405, never a 200).
Plus: RBAC deny-cell (`view_audit_log`), admin-session-only, pagination + every filter, the CSV
export extract, and a non-admin (staff) session denied.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DatabaseError

import src.application.auth.admin as admin_mod
from src.application.school_admin import services as school_admin_svc
from src.constants.enums import AdminRole
from src.domain.compliance.models import AuditLog

ENDPOINT = "/api/v1/admin/audit-log"
EXPORT_ENDPOINT = "/api/v1/admin/audit-log/export"
SIGNIN = "/api/v1/admin/sign-in"


def _complete_signin(client, monkeypatch, email="admin@youhue.app", password="Password123"):
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        admin_mod, "send_email", lambda to, subject, body: captured.update(body=body)
    )
    client.post(SIGNIN, json={"email": email, "password": password})
    code = captured["body"].split()[-1]
    return client.post(
        SIGNIN, json={"email": email, "password": password, "mfa_code": code}
    ).json()["admin_session"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- Scenario 1 (positive): view + filter the audit log ---------------------------------------

def test_admin_views_the_audit_log(client, db, make_admin, monkeypatch):
    # `_complete_signin` below itself writes its own "admin.sign_in" audit row (sign-in IS an
    # audited admin action) — this fixture row is isolated from it by an `action` filter, same as
    # a real admin applying a filter (Scenario 1's "they open the viewer and apply a filter").
    make_admin(role=AdminRole.superadmin)
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fr_16_02.config_changed", target="school:x"))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"action": "config_changed"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    entry = body["entries"][0]
    assert {"at", "actor_id", "action"} <= entry.keys()
    assert entry["action"] == "fr_16_02.config_changed"


def test_filter_by_action_substring(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fr_19_02.support_access", target="school:a"))
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fr_19_05.update", target="word_list"))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"action": "support_access"}, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["entries"][0]["action"] == "fr_19_02.support_access"


def test_filter_by_actor_id(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    actor = uuid.uuid4()
    db.add(AuditLog(actor_id=actor, action="a", target="t"))
    db.add(AuditLog(actor_id=uuid.uuid4(), action="b", target="t"))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"actor_id": str(actor)}, headers=_auth(token))
    assert r.json()["total"] == 1


def test_filter_by_school_id(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    db.add(AuditLog(actor_id=uuid.uuid4(), action="a", target="t", school_id=school.id))
    db.add(AuditLog(actor_id=uuid.uuid4(), action="b", target="t"))  # platform-level, school_id null
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"school_id": str(school.id)}, headers=_auth(token))
    assert r.json()["total"] == 1


def test_filter_by_date_range(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    now = datetime.now(UTC)
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fx.old", target="t", at=now - timedelta(days=10)))
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fx.recent", target="t", at=now))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(
        ENDPOINT,
        params={"action": "fx.", "date_from": (now - timedelta(days=1)).isoformat()},
        headers=_auth(token),
    )
    body = r.json()
    assert body["total"] == 1
    assert body["entries"][0]["action"] == "fx.recent"


def test_pagination(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    for i in range(5):
        db.add(AuditLog(actor_id=uuid.uuid4(), action=f"fx.page.{i}", target="t"))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(
        ENDPOINT, params={"action": "fx.page.", "page": 1, "page_size": 2}, headers=_auth(token)
    )
    body = r.json()
    assert body["total"] == 5
    assert len(body["entries"]) == 2
    assert body["page"] == 1
    assert body["page_size"] == 2


def test_entries_are_newest_first(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    now = datetime.now(UTC)
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fx.older", target="t", at=now - timedelta(hours=1)))
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fx.newer", target="t", at=now))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"action": "fx."}, headers=_auth(token))
    actions = [e["action"] for e in r.json()["entries"]]
    assert actions == ["fx.newer", "fx.older"]


# ---- Scenario 2 (positive): support access writes a queryable, immutable audit entry ----------

def test_support_access_entry_is_visible_in_the_viewer(client, db, make_admin, make_school, monkeypatch):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    school_admin_svc.support_access(db, admin, school.id, reason="Ticket #4821 — alert routing")
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"action": "support_access"}, headers=_auth(token))
    body = r.json()
    assert body["total"] == 1
    entry = body["entries"][0]
    assert entry["action"] == "fr_19_02.support_access"
    assert entry["target"] == f"school:{school.id}"
    assert entry["actor_id"] == str(admin.id)
    # The reason TEXT itself is never persisted on the audit row (FR-19-02's own documented
    # decision — no free-text field on `audit_logs`; matches the approved SC-080 screen's
    # When/Actor/Action-only table, no Reason column) — the entry's mere existence, tied to the
    # actor and target school, is the accountability record.
    assert "reason" not in entry


# ---- Scenario 3 (NEG): immutable — no update/delete path, DB trigger blocks it directly --------

def test_audit_log_cannot_be_mutated_at_the_db_level(db):
    db.add(AuditLog(actor_id=uuid.uuid4(), action="x", target="y"))
    db.commit()
    with pytest.raises(DatabaseError):  # append-only trigger (c0ffee000001) blocks UPDATE
        db.query(AuditLog).update({AuditLog.action: "hacked"})
        db.flush()
    db.rollback()


# ---- Scenario 4 (NEG): the viewer is read + filter + export only, never an editor --------------

def test_no_write_route_exists_on_audit_log(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    token = _complete_signin(client, monkeypatch)
    for method in ("post", "patch", "put", "delete"):
        r = getattr(client, method)(ENDPOINT, headers=_auth(token))
        assert r.status_code == 405, f"{method.upper()} must not be a route on {ENDPOINT}"


# ---- Export -------------------------------------------------------------------------------------

def test_export_returns_a_csv_of_the_filtered_rows(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fr_19_02.support_access", target="school:a"))
    db.add(AuditLog(actor_id=uuid.uuid4(), action="fr_19_05.update", target="word_list"))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(EXPORT_ENDPOINT, params={"action": "support_access"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    body = r.text
    assert "fr_19_02.support_access" in body
    assert "fr_19_05.update" not in body


# ---- RBAC deny-cell + session posture -----------------------------------------------------------

def test_denied_for_role_without_view_audit_log(client, db, make_admin, monkeypatch):
    import src.application.authz.admin as admin_authz_mod

    make_admin(role=AdminRole.support)
    monkeypatch.setitem(admin_authz_mod._ROLE_PERMISSIONS, AdminRole.support, frozenset())
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403
    row = db.query(AuditLog).filter(
        AuditLog.action == "admin.rbac.denied:view_audit_log"
    ).one_or_none()
    assert row is not None  # the denial itself is audit-logged, never a silent 403


def test_requires_admin_session(client, db, make_school, make_staff):
    school = make_school()
    staff = make_staff(school)
    token = client.post(
        "/api/v1/auth/staff/sign-in", json={"email": staff.email, "password": "Password123"}
    ).json()["access_token"]
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403


def test_no_filter_matches_returns_zero_total_not_an_error(client, db, make_admin, monkeypatch):
    # A filter matching nothing is a valid, renderable empty state (200), never an error — the
    # log can never be truly empty through this authenticated flow since sign-in itself is
    # audited (`admin.sign_in`, above), so this exercises the empty state the real viewer hits
    # whenever a filter has no matches.
    make_admin(role=AdminRole.superadmin)
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, params={"action": "no-such-action"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"entries": [], "total": 0, "page": 1, "page_size": 50}
