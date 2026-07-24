"""FR-19-02 — admin console: manage school accounts/trial settings, grant ONE 14-day trial
extension, and permission-bound audited support access to a school's child data.

Covers @FR-19-02:
  Scenario 1 (positive): update a school's account/trial settings -> the changes are applied.
  Scenario 2 (pos + NEG): grant the trial extension once; a second attempt is refused (409).
  Scenario 3 (pos + NEG): support access to a school's child data is permission-bound (403 for a
    role lacking `access_child_data`) and audit-logged (fr_19_02.support_access).
Plus: RBAC deny-cells for `extend_trial`/`update_account` (`manage_accounts`), 404 for an unknown
school, 422 for a blank support-access reason, and the immutable audit trail's shape.
"""
import pytest
from fastapi import HTTPException

import src.application.auth.admin as admin_mod
from src.application.school_admin import services as school_admin_svc
from src.constants.enums import AdminRole, SchoolStatus, SchoolTier, SubscriptionState
from src.domain.billing import services as billing_db
from src.domain.compliance.models import AuditLog

ENDPOINT = "/api/v1/admin/schools"
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


# ---- Scenario 1: manage a school account and its trial settings (positive) -----------------

def test_update_account_applies_changes_to_the_school(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    updated, _ = school_admin_svc.update_account(
        db, admin, school.id, tier=SchoolTier.premium, account_status=SchoolStatus.active
    )
    db.commit()
    assert updated.tier == SchoolTier.premium
    assert updated.status == SchoolStatus.active


def test_update_account_endpoint_applies_and_returns_the_school(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    token = _complete_signin(client, monkeypatch)
    r = client.patch(
        f"{ENDPOINT}/{school.id}",
        json={"action": "update_account", "tier": "premium"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "update_account"
    assert body["school"]["tier"] == "premium"


def test_update_account_leaves_unset_fields_unchanged(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    original_timezone = school.timezone
    school_admin_svc.update_account(db, admin, school.id, tier=SchoolTier.premium)
    db.commit()
    assert school.timezone == original_timezone  # untouched


def test_update_account_unknown_school_is_404(db, make_admin):
    import uuid

    admin = make_admin(role=AdminRole.superadmin)
    with pytest.raises(HTTPException) as exc:
        school_admin_svc.update_account(db, admin, uuid.uuid4(), tier=SchoolTier.premium)
    assert exc.value.status_code == 404


# ---- Scenario 2: grant a single 14-day trial extension (pos + NEG second refused) -----------

def test_extend_trial_extends_by_14_days_and_records_the_count(db, make_admin, make_school):
    from datetime import UTC, datetime

    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    before = datetime.now(UTC)
    _, sub = school_admin_svc.extend_trial(db, admin, school.id)
    db.commit()
    assert sub.trial_extension_count == 1
    assert sub.trial_end_at is not None
    assert (sub.trial_end_at - before).days >= 13  # ~14 days out


def test_second_extension_attempt_is_refused_409(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    school_admin_svc.extend_trial(db, admin, school.id)
    db.commit()
    with pytest.raises(HTTPException) as exc:
        school_admin_svc.extend_trial(db, admin, school.id)
    assert exc.value.status_code == 409
    assert "already used" in exc.value.detail


def test_extend_trial_endpoint_then_second_attempt_409(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    token = _complete_signin(client, monkeypatch)
    auth = {"Authorization": f"Bearer {token}"}
    first = client.patch(f"{ENDPOINT}/{school.id}", json={"action": "extend_trial"}, headers=auth)
    assert first.status_code == 200
    assert first.json()["school"]["trial_extension_count"] == 1

    second = client.patch(f"{ENDPOINT}/{school.id}", json={"action": "extend_trial"}, headers=auth)
    assert second.status_code == 409


def test_extend_trial_creates_a_subscription_row_lazily(db, make_admin, make_school):
    """No FR-17-01/03 registration-time creator exists yet — the first trial touch creates it."""
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    assert billing_db.get_subscription(db, school.id) is None
    school_admin_svc.extend_trial(db, admin, school.id)
    db.commit()
    sub = billing_db.get_subscription(db, school.id)
    assert sub is not None
    assert sub.state == SubscriptionState.trial  # column default, untouched by the extension


def test_extend_trial_extends_from_existing_trial_end_at(db, make_admin, make_school):
    from datetime import UTC, datetime, timedelta

    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    sub = billing_db.get_or_create_subscription(db, school.id)
    anchor = datetime.now(UTC) + timedelta(days=5)
    sub.trial_end_at = anchor
    db.commit()
    _, updated = school_admin_svc.extend_trial(db, admin, school.id)
    db.commit()
    assert updated.trial_end_at == anchor + timedelta(days=14)


# ---- Scenario 3: support access is permission-bound + audit-logged (pos + NEG) --------------

def test_support_access_granted_with_reason_is_audit_logged(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    school_admin_svc.support_access(db, admin, school.id, reason="Ticket #4821 — alert routing")
    db.commit()
    rows = db.query(AuditLog).filter(AuditLog.action == "fr_19_02.support_access").all()
    assert len(rows) == 1
    assert rows[0].actor_id == admin.id
    assert rows[0].target == f"school:{school.id}"
    assert rows[0].school_id == school.id
    assert rows[0].at is not None


def test_support_access_requires_a_reason(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    with pytest.raises(HTTPException) as exc:
        school_admin_svc.support_access(db, admin, school.id, reason="   ")
    assert exc.value.status_code == 422


@pytest.mark.authz
def test_support_access_denied_for_role_without_access_child_data(db, make_admin, make_school):
    """Support-access is its OWN permission, narrower than `manage_accounts` — a `support`-role
    actor has `manage_accounts` (can manage the account) but NOT `access_child_data` (cannot open
    a school's children's data). The denial is written to the immutable audit log first."""
    support = make_admin(email="support@youhue.app", role=AdminRole.support)
    school = make_school()
    with pytest.raises(HTTPException) as exc:
        school_admin_svc.support_access(db, support, school.id, reason="ticket #1")
    assert exc.value.status_code == 403
    db.commit()
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "admin.rbac.denied:access_child_data")
        .all()
    )
    assert len(rows) == 1 and rows[0].target == "admin_console" and rows[0].school_id is None


@pytest.mark.authz
def test_support_access_endpoint_forbidden_for_support_role(client, make_admin, make_school, monkeypatch):
    make_admin(email="support@youhue.app", role=AdminRole.support)
    school = make_school()
    token = _complete_signin(client, monkeypatch, email="support@youhue.app")
    r = client.patch(
        f"{ENDPOINT}/{school.id}",
        json={"action": "support_access", "reason": "ticket #1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 403


def test_support_access_endpoint_permitted_for_superadmin(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    token = _complete_signin(client, monkeypatch)
    r = client.patch(
        f"{ENDPOINT}/{school.id}",
        json={"action": "support_access", "reason": "ticket #4821 — alert routing"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    rows = db.query(AuditLog).filter(AuditLog.action == "fr_19_02.support_access").all()
    assert len(rows) == 1


# ---- RBAC deny-cells for extend_trial / update_account (manage_accounts) --------------------

@pytest.mark.authz
def test_extend_trial_denied_for_role_without_manage_accounts(db, make_admin, make_school, monkeypatch):
    """`support` DOES have `manage_accounts` in the FR-19-01 matrix, so exercise the true deny-cell
    with a role that lacks it entirely by revoking it for this check."""
    from src.application.authz import admin as admin_authz

    support = make_admin(email="support@youhue.app", role=AdminRole.support)
    school = make_school()
    monkeypatch.setitem(
        admin_authz._ROLE_PERMISSIONS, AdminRole.support, frozenset()
    )
    with pytest.raises(HTTPException) as exc:
        school_admin_svc.extend_trial(db, support, school.id)
    assert exc.value.status_code == 403
    db.commit()
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "admin.rbac.denied:manage_accounts")
        .all()
    )
    assert len(rows) == 1


def test_support_role_can_manage_accounts_and_extend_trial(client, db, make_admin, make_school, monkeypatch):
    """`support` DOES hold `manage_accounts` — it can update accounts and extend trials; only
    `access_child_data` (support_access) is out of its reach."""
    make_admin(email="support@youhue.app", role=AdminRole.support)
    school = make_school()
    token = _complete_signin(client, monkeypatch, email="support@youhue.app")
    r = client.patch(
        f"{ENDPOINT}/{school.id}",
        json={"action": "extend_trial"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200


def test_endpoint_requires_admin_session(client, make_school):
    school = make_school()
    r = client.patch(f"{ENDPOINT}/{school.id}", json={"action": "extend_trial"})
    assert r.status_code == 403


# ---- listing / detail reads -------------------------------------------------------------------

def test_list_schools_returns_every_school(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    make_school(code="A1", name="Oakfield Primary")
    make_school(code="A2", name="Riverside")
    rows = school_admin_svc.list_schools(db, admin)
    assert {s.name for s in rows} == {"Oakfield Primary", "Riverside"}


def test_list_schools_endpoint(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    make_school()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["count"] == 1


def test_get_school_detail_reflects_trial_state(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    token = _complete_signin(client, monkeypatch)
    auth = {"Authorization": f"Bearer {token}"}
    client.patch(f"{ENDPOINT}/{school.id}", json={"action": "extend_trial"}, headers=auth)
    r = client.get(f"{ENDPOINT}/{school.id}", headers=auth)
    assert r.status_code == 200
    body = r.json()
    assert body["trial_extension_count"] == 1
    assert body["trial_end_at"] is not None


def test_get_school_detail_no_subscription_yet_is_null_not_error(client, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    token = _complete_signin(client, monkeypatch)
    r = client.get(f"{ENDPOINT}/{school.id}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["trial_extension_count"] == 0
    assert body["trial_end_at"] is None
    assert body["subscription_state"] is None
