"""Staff sign-in, generic errors, forgot/reset, and email-OTP MFA (INFRA-01 / FR-01-*)."""
import src.application.auth.staff as staff_mod
from src.domain.enums import StaffRole

SIGNIN = "/api/v1/auth/staff/sign-in"


def test_staff_signin_success(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    r = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "Password123"})
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"]
    assert body["mfa_required"] is False


def test_wrong_password_and_unknown_email_are_identical(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    wrong = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "nope"})
    ghost = client.post(SIGNIN, json={"email": "ghost@oakwood.edu", "password": "whatever"})
    assert wrong.status_code == 401
    assert ghost.status_code == 401
    assert wrong.json()["detail"] == ghost.json()["detail"]  # no account-existence disclosure


def test_forgot_password_no_disclosure(client):
    r = client.post("/api/v1/auth/staff/forgot-password", json={"email": "ghost@nowhere.edu"})
    assert r.status_code == 202  # unknown email still succeeds silently


def test_forgot_then_reset_rotates_password(client, make_school, make_staff, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    make_staff(make_school(), email="t@oakwood.edu", password="OldPass123")

    assert client.post("/api/v1/auth/staff/forgot-password", json={"email": "t@oakwood.edu"}).status_code == 202
    token = captured["body"].split("token=")[-1].strip()
    assert client.post("/api/v1/auth/staff/reset-password", json={"token": token, "new_password": "NewPass123"}).status_code == 204

    assert client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "OldPass123"}).status_code == 401
    assert client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "NewPass123"}).status_code == 200


def test_reset_token_single_use(client, make_school, make_staff, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    make_staff(make_school(), email="t@oakwood.edu", password="OldPass123")
    client.post("/api/v1/auth/staff/forgot-password", json={"email": "t@oakwood.edu"})
    token = captured["body"].split("token=")[-1].strip()
    assert client.post("/api/v1/auth/staff/reset-password", json={"token": token, "new_password": "NewPass123"}).status_code == 204
    # reusing the consumed token fails
    assert client.post("/api/v1/auth/staff/reset-password", json={"token": token, "new_password": "Another123"}).status_code == 400


def test_mfa_challenge_then_verify(client, make_school, make_staff, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    make_staff(make_school(), email="head@oakwood.edu", password="Password123",
               role=StaffRole.leadership, mfa=True)

    signin = client.post(SIGNIN, json={"email": "head@oakwood.edu", "password": "Password123"})
    assert signin.status_code == 200
    assert signin.json()["mfa_required"] is True
    pending_token = signin.json()["access_token"]

    # the mfa-pending session cannot reach a protected route yet
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {pending_token}"}).status_code == 401

    code = captured["body"].split()[-1]
    verify = client.post("/api/v1/auth/staff/mfa/verify", json={"session_token": pending_token, "code": code})
    assert verify.status_code == 200
    full_token = verify.json()["access_token"]
    assert client.get("/api/v1/me", headers={"Authorization": f"Bearer {full_token}"}).status_code == 200
