"""FR-20-07 — cross-tenant isolation re-verification for features merged since INFRA-02's
baseline isolation suite (``tests/test_isolation.py``).

INFRA-02 already covers: student read (own class / same-school wrong class / student session /
cross-school), audit-log immutability, `isolation.get_scoped`/`get_scoped_via_student`. Roster
import/reconcile, calendar, and notifications already carry their own explicit cross-school tests
(`test_roster.py`, `test_roster_reconcile.py`, `test_calendar.py`, `test_notifications.py`). This
file closes the one real gap found: FR-02-03 (shared-class colleague invitations) shipped with
zero cross-school coverage — a repo-wide grep for `school_b`/`cross.school`/`other_school` in
`test_invitations.py` returned no hits before this file was added.

`/check-ins/config` (FR-04-03) and `/check-ins/sync` (FR-04-06) are NOT re-tested here: both are
structurally self-scoped (the caller's own `StudentDep`, no ID parameter reaching another
student's or school's data), so there is no cross-tenant vector to exercise — confirmed by reading
`src/routers/checkins.py` (neither endpoint accepts a student/school identifier from the caller).
"""
import pytest

from src.constants.enums import StaffClassScope

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str, pw: str = "Password123") -> str:
    return client.post(SIGNIN, json={"email": email, "password": pw}).json()["access_token"]


def _owner_at_school_a(client, make_school, make_staff, make_class, grant_class_access):
    school_a = make_school(code="XT-A")
    owner = make_staff(school_a, email="owner@oakwood.edu")
    klass = make_class(school_a, name="Room 3A")
    grant_class_access(owner, klass, scope=StaffClassScope.owner)
    token = _staff_token(client, "owner@oakwood.edu")
    return school_a, klass, token


@pytest.mark.authz
def test_list_invitations_cross_school_denied(
    client, make_school, make_staff, make_class, grant_class_access
):
    """A School-B class-owner must get nothing back for School A's class invitations list."""
    school_a, klass_a, _ = _owner_at_school_a(client, make_school, make_staff, make_class, grant_class_access)
    make_school(code="XT-B")  # school_b exists in the platform, unrelated to the caller below
    school_b_owner_school = make_school(code="XT-B2")
    make_staff(school_b_owner_school, email="intruder@oakwood.edu")
    intruder_token = _staff_token(client, "intruder@oakwood.edu")

    r = client.get(f"/api/v1/classes/{klass_a.id}/invitations", headers=_auth(intruder_token))
    assert r.status_code in (403, 404)  # never 200, never leaks School A's pending invitations


@pytest.mark.authz
def test_invite_colleague_cross_school_denied(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    """A School-B staff member cannot invite anyone into a School-A class."""
    import src.application.invitations.services as invitations_mod

    monkeypatch.setattr(invitations_mod, "send_email", lambda *a, **k: None)
    school_a, klass_a, _ = _owner_at_school_a(client, make_school, make_staff, make_class, grant_class_access)
    school_b = make_school(code="XT-C")
    make_staff(school_b, email="intruder2@oakwood.edu")
    intruder_token = _staff_token(client, "intruder2@oakwood.edu")

    r = client.post(
        f"/api/v1/classes/{klass_a.id}/invitations",
        json={"email": "victim@oakwood.edu"},
        headers=_auth(intruder_token),
    )
    assert r.status_code in (403, 404)


@pytest.mark.authz
def test_action_on_invitation_cross_school_denied(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    """A School-B staff member cannot resend or revoke a School-A invitation, even knowing its
    real invitation_id — the mechanism must reject on `invitation.school_id != staff.school_id`,
    not merely on not being the class owner (the same school, wrong owner case is already covered
    by `test_invitations.py::test_invite_forbidden_not_owner`)."""
    import src.application.invitations.services as invitations_mod

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        invitations_mod, "send_email", lambda to, subject, body: captured.__setitem__(to, body)
    )
    school_a, klass_a, owner_token = _owner_at_school_a(
        client, make_school, make_staff, make_class, grant_class_access
    )
    invite_resp = client.post(
        f"/api/v1/classes/{klass_a.id}/invitations",
        json={"email": "colleague@oakwood.edu"},
        headers=_auth(owner_token),
    )
    assert invite_resp.status_code == 201
    invitation_id = invite_resp.json()["invitation_id"]

    school_b = make_school(code="XT-D")
    make_staff(school_b, email="intruder3@oakwood.edu")
    intruder_token = _staff_token(client, "intruder3@oakwood.edu")

    revoke_r = client.post(
        f"/api/v1/invitations/{invitation_id}/action",
        json={"action": "revoke"},
        headers=_auth(intruder_token),
    )
    assert revoke_r.status_code in (403, 404)

    resend_r = client.post(
        f"/api/v1/invitations/{invitation_id}/action",
        json={"action": "resend"},
        headers=_auth(intruder_token),
    )
    assert resend_r.status_code in (403, 404)

    # the real invitation is untouched — still pending, resolvable by its original owner
    list_r = client.get(f"/api/v1/classes/{klass_a.id}/invitations", headers=_auth(owner_token))
    assert list_r.status_code == 200
    assert list_r.json()["invitations"][0]["status"] in ("invited", "sent")
