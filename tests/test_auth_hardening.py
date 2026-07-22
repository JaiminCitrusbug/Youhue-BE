"""Hardening tests from the fresh-eyes review: revoke-on-password-change, OTP brute-force cap,
expired reset token, and rate limiting."""
from datetime import UTC, datetime, timedelta

import src.application.auth.staff as staff_mod
from config.env_config import settings
from src.constants.enums import StaffRole
from src.domain.auth.models import PasswordResetToken

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_password_change_revokes_existing_sessions(client, make_school, make_staff, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    make_staff(make_school(), email="t@oakwood.edu", password="OldPass123")
    token = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "OldPass123"}).json()["access_token"]
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 200

    client.post("/api/v1/auth/staff/forgot-password", json={"email": "t@oakwood.edu"})
    reset_token = captured["body"].split("token=")[-1].strip()
    assert client.post("/api/v1/auth/staff/reset-password", json={"token": reset_token, "new_password": "NewPass123"}).status_code == 204
    # the pre-existing session must be dead after a password change
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 401


def test_wrong_otp_rejected_then_capped(client, make_school, make_staff, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    make_staff(make_school(), email="head@oakwood.edu", password="Password123", role=StaffRole.leadership, mfa=True)
    pending = client.post(SIGNIN, json={"email": "head@oakwood.edu", "password": "Password123"}).json()["access_token"]
    real_code = captured["body"].split()[-1]
    wrong_code = "111111" if real_code == "000000" else "000000"

    for _ in range(settings.mfa_max_attempts):
        r = client.post("/api/v1/auth/staff/mfa/verify", json={"session_token": pending, "code": wrong_code})
        assert r.status_code == 401
    # cap reached -> even the CORRECT code is now refused
    capped = client.post("/api/v1/auth/staff/mfa/verify", json={"session_token": pending, "code": real_code})
    assert capped.status_code == 401


def test_expired_reset_token_rejected(client, db, make_school, make_staff, monkeypatch):
    captured: dict[str, str] = {}
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: captured.update(body=body))
    make_staff(make_school(), email="t@oakwood.edu", password="OldPass123")
    client.post("/api/v1/auth/staff/forgot-password", json={"email": "t@oakwood.edu"})
    token = captured["body"].split("token=")[-1].strip()
    # force-expire the token
    row = db.query(PasswordResetToken).first()
    row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    db.commit()
    r = client.post("/api/v1/auth/staff/reset-password", json={"token": token, "new_password": "NewPass123"})
    assert r.status_code == 400


def test_rate_limit_returns_429(client, make_school, make_staff, monkeypatch):
    monkeypatch.setattr(settings, "rate_limit_signin_per_minute", 3)
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    for _ in range(3):
        client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "wrong"})
    assert client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "wrong"}).status_code == 429
