"""Logout / token revocation, /me identity, invalid-token rejection (INFRA-01)."""
from src.domain.enums import StaffRole

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _signin(client, email="t@oakwood.edu", password="Password123") -> str:
    return client.post(SIGNIN, json={"email": email, "password": password}).json()["access_token"]


def test_logout_revokes_session(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    token = _signin(client)
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=_auth(token)).status_code == 204
    assert client.get("/api/v1/me", headers=_auth(token)).status_code == 401  # revoked


def test_me_returns_identity(client, make_school, make_staff):
    school = make_school()
    # support role: not MFA-forced, so the session is usable immediately
    make_staff(school, email="co@oakwood.edu", password="Password123", role=StaffRole.support)
    me = client.get("/api/v1/me", headers=_auth(_signin(client, "co@oakwood.edu"))).json()
    assert me["kind"] == "staff"
    assert me["role"] == "support"
    assert me["school_id"] == str(school.id)


def test_invalid_token_rejected(client):
    assert client.get("/api/v1/me", headers=_auth("not-a-real-token")).status_code == 401


def test_missing_token_rejected(client):
    assert client.get("/api/v1/me").status_code == 403  # HTTPBearer -> 403 without credentials
