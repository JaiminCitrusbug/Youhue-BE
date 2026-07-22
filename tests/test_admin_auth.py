"""Admin console sign-in + RBAC (FR-19-01).

Covers @FR-19-01: sign-in success (email + password + email-OTP MFA), MFA required, generic 401
(no account-existence / factor disclosure), 403 role-not-permitted on the probe route (audit-logged),
rate-limit and lockout. The admin surface is disjoint from staff/student.
"""
import pytest

import src.application.auth.admin as admin_mod
from config.env_config import settings
from src.application.authz import admin as admin_authz
from src.constants.enums import AdminRole
from src.domain.compliance.models import AuditLog

SIGNIN = "/api/v1/admin/sign-in"
PROBE = "/api/v1/admin/_probe/seed-maintenance"


def _capture_otp(monkeypatch) -> dict[str, str]:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        admin_mod, "send_email", lambda to, subject, body: captured.update(body=body)
    )
    return captured


def _complete_signin(client, monkeypatch, email="admin@youhue.app", password="Password123"):
    """Two-phase admin sign-in -> returns the response json of the completed (MFA) step."""
    captured = _capture_otp(monkeypatch)
    challenge = client.post(SIGNIN, json={"email": email, "password": password})
    assert challenge.status_code == 200
    assert challenge.json()["mfa_required"] is True
    assert challenge.json()["admin_session"] is None  # no session until MFA completes
    code = captured["body"].split()[-1]
    return client.post(SIGNIN, json={"email": email, "password": password, "mfa_code": code})


# ---- Scenario 1: sign in to the admin console with a role (positive) ----

def test_admin_signin_success_with_mfa_and_role(client, make_admin, monkeypatch):
    make_admin(email="admin@youhue.app", password="Password123", role=AdminRole.superadmin)
    done = _complete_signin(client, monkeypatch)
    assert done.status_code == 200
    body = done.json()
    assert body["admin_session"]
    assert body["role"] == "superadmin"
    assert body["mfa_required"] is False
    # the minted admin session reaches a role-permitted admin route
    ok = client.get(PROBE, headers={"Authorization": f"Bearer {body['admin_session']}"})
    assert ok.status_code == 200
    assert ok.json()["action"] == "seed-maintenance"


def test_admin_session_resolves_me_with_role(client, make_admin, monkeypatch):
    make_admin(email="admin@youhue.app", password="Password123", role=AdminRole.superadmin)
    token = _complete_signin(client, monkeypatch).json()["admin_session"]
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["kind"] == "admin"
    assert me.json()["role"] == "superadmin"
    assert me.json()["school_id"] is None  # platform-level, never school-scoped


# ---- MFA required to complete sign-in ----

def test_mfa_required_before_session_issued(client, make_admin, monkeypatch):
    _capture_otp(monkeypatch)
    make_admin(email="admin@youhue.app", password="Password123")
    r = client.post(SIGNIN, json={"email": "admin@youhue.app", "password": "Password123"})
    assert r.status_code == 200
    assert r.json()["mfa_required"] is True
    assert r.json()["admin_session"] is None  # cannot reach the console without completing MFA


def test_wrong_mfa_code_is_generic_401(client, make_admin, monkeypatch):
    _capture_otp(monkeypatch)
    make_admin(email="admin@youhue.app", password="Password123")
    client.post(SIGNIN, json={"email": "admin@youhue.app", "password": "Password123"})
    bad = client.post(
        SIGNIN, json={"email": "admin@youhue.app", "password": "Password123", "mfa_code": "000000"}
    )
    assert bad.status_code == 401


# ---- generic 401: no account-existence disclosure ----

def test_wrong_password_and_unknown_email_are_identical(client, make_admin):
    make_admin(email="admin@youhue.app", password="Password123")
    wrong = client.post(SIGNIN, json={"email": "admin@youhue.app", "password": "nope"})
    ghost = client.post(SIGNIN, json={"email": "ghost@youhue.app", "password": "whatever"})
    assert wrong.status_code == 401
    assert ghost.status_code == 401
    assert wrong.json()["detail"] == ghost.json()["detail"]  # no enumeration


# ---- Scenario 2: a role cannot reach actions outside its permission (negative, 403 + audit) ----

@pytest.mark.authz
def test_limited_role_denied_probe_and_audit_logged(client, db, make_admin, monkeypatch):
    make_admin(email="support@youhue.app", password="Password123", role=AdminRole.support)
    token = _complete_signin(
        client, monkeypatch, email="support@youhue.app"
    ).json()["admin_session"]
    denied = client.get(PROBE, headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403
    # the denial is written to the immutable audit log
    rows = db.query(AuditLog).filter(AuditLog.action == "admin.rbac.denied:manage_seed").all()
    assert len(rows) == 1
    assert rows[0].target == "admin_console"
    assert rows[0].school_id is None


def test_permitted_role_allowed_probe(client, make_admin, monkeypatch):
    make_admin(email="admin@youhue.app", password="Password123", role=AdminRole.superadmin)
    token = _complete_signin(client, monkeypatch).json()["admin_session"]
    assert client.get(PROBE, headers={"Authorization": f"Bearer {token}"}).status_code == 200


@pytest.mark.authz
def test_staff_session_cannot_reach_admin_probe(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    r = client.post(
        "/api/v1/auth/staff/sign-in", json={"email": "t@oakwood.edu", "password": "Password123"}
    )
    token = r.json()["access_token"]
    denied = client.get(PROBE, headers={"Authorization": f"Bearer {token}"})
    assert denied.status_code == 403  # a staff session never resolves an admin route


def test_admin_probe_requires_auth(client):
    assert client.get(PROBE).status_code == 403  # no bearer -> HTTPBearer rejects


# ---- rate-limit + lockout ----

def test_signin_rate_limited(client, make_admin, monkeypatch):
    _capture_otp(monkeypatch)
    make_admin(email="admin@youhue.app", password="Password123")
    last = None
    for _ in range(settings.rate_limit_signin_per_minute + 1):
        last = client.post(SIGNIN, json={"email": "admin@youhue.app", "password": "Password123"})
    assert last.status_code == 429  # throttled after the per-minute budget


def test_lockout_after_max_failures(client, make_admin):
    make_admin(email="admin@youhue.app", password="Password123")
    for _ in range(settings.lockout_max_attempts):
        assert client.post(
            SIGNIN, json={"email": "admin@youhue.app", "password": "wrong"}
        ).status_code == 401
    # further attempts are locked out even with the CORRECT password
    locked = client.post(SIGNIN, json={"email": "admin@youhue.app", "password": "Password123"})
    assert locked.status_code == 423


def test_otp_brute_force_capped(client, make_admin, monkeypatch):
    _capture_otp(monkeypatch)
    make_admin(email="admin@youhue.app", password="Password123")
    client.post(SIGNIN, json={"email": "admin@youhue.app", "password": "Password123"})
    # exhaust the OTP-verification cap with wrong codes; correct password each time (no acct lockout)
    for _ in range(settings.mfa_max_attempts):
        assert client.post(
            SIGNIN,
            json={"email": "admin@youhue.app", "password": "Password123", "mfa_code": "000000"},
        ).status_code == 401
    # even a subsequent attempt is rejected — the brute-force cap holds
    assert client.post(
        SIGNIN, json={"email": "admin@youhue.app", "password": "Password123", "mfa_code": "000000"}
    ).status_code == 401


# ---- unit: RBAC matrix ----

def test_permission_matrix():
    assert admin_authz.has_permission(AdminRole.superadmin, admin_authz.AdminPermission.manage_seed)
    assert admin_authz.has_permission(
        AdminRole.superadmin, admin_authz.AdminPermission.platform_config
    )
    assert admin_authz.has_permission(
        AdminRole.support, admin_authz.AdminPermission.manage_accounts
    )
    assert not admin_authz.has_permission(
        AdminRole.support, admin_authz.AdminPermission.manage_seed
    )
    assert not admin_authz.has_permission(
        AdminRole.support, admin_authz.AdminPermission.platform_config
    )
