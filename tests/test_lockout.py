"""Account lockout after N consecutive failures (INFRA-01 / FR-01-08 -> 423)."""
from src.config import settings

SIGNIN = "/api/v1/auth/staff/sign-in"


def test_lockout_after_max_attempts(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123")
    for _ in range(settings.lockout_max_attempts):
        r = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "wrong"})
        assert r.status_code == 401
    # further attempts are locked out — even with the CORRECT password
    locked = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "Password123"})
    assert locked.status_code == 423
