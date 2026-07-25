"""Shared-class colleague invitations (FR-02-03).

Every ticket Must-not has a test that fails if the guarantee is removed:

  NEG (GATE G-4)  An invited colleague reaches ONLY the shared class and its students, never the
                  whole school.              -> test_accepted_colleague_reaches_only_shared_class
  NEG             A superseded (revoked/resent) invitation token stops working.
                  -> test_revoke_kills_the_token / test_resend_supersedes_old_token
  Only the class owner may invite (403 otherwise).      -> test_invite_forbidden_not_owner
  An already-accepted invitation cannot be re-actioned (409).  -> test_action_on_accepted_409
"""
import src.application.invitations.services as invitations_mod
from src.constants.enums import StaffClassScope, StaffRole, StaffStatus
from src.domain.org.models import StaffClassAccess

SIGNIN = "/api/v1/auth/staff/sign-in"


def _invite_url(class_id: object) -> str:
    return f"/api/v1/classes/{class_id}/invitations"


def _action_url(invitation_id: object) -> str:
    return f"/api/v1/invitations/{invitation_id}/action"


ACCEPT_URL = "/api/v1/invitations/accept"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _owner_signed_in(client, monkeypatch, make_school, make_staff, make_class, grant_class_access):
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        invitations_mod, "send_email", lambda to, subject, body: captured.__setitem__(to, body)
    )
    school = make_school(code="INV-1")
    owner = make_staff(school, email="owner@oakwood.edu", password="Password123")
    klass = make_class(school, name="3A")
    grant_class_access(owner, klass, scope=StaffClassScope.owner)
    signin = client.post(SIGNIN, json={"email": "owner@oakwood.edu", "password": "Password123"})
    token = signin.json()["access_token"]
    return school, owner, klass, token, captured


def _accept_link_token(body: str) -> str:
    return body.split("token=")[-1].strip().rstrip(")")


def test_my_classes_lists_only_owned(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    shared_only = make_class(school, name="4C")
    grant_class_access(owner, shared_only, scope=StaffClassScope.shared)

    r = client.get("/api/v1/classes/mine", headers=_auth(token))
    assert r.status_code == 200
    names = [c["name"] for c in r.json()["classes"]]
    assert names == ["3A"]  # owned only — the shared-scope class is excluded


def test_list_class_invitations_shows_real_rows(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))

    r = client.get(_invite_url(klass.id), headers=_auth(token))
    assert r.status_code == 200
    rows = r.json()["invitations"]
    assert len(rows) == 1
    assert rows[0]["email"] == "colleague@oakwood.edu"
    assert rows[0]["status"] == "sent"


def test_list_class_invitations_forbidden_not_owner(client, make_school, make_staff, make_class):
    school = make_school(code="INV-3")
    make_staff(school, email="t@oakwood.edu", password="Password123")
    klass = make_class(school, name="3C")
    signin = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "Password123"})
    token = signin.json()["access_token"]
    r = client.get(_invite_url(klass.id), headers=_auth(token))
    assert r.status_code == 403


def test_invite_sends_single_use_token(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    r = client.post(
        _invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "sent"
    assert body["invitation_id"]
    assert "colleague@oakwood.edu" in captured


def test_invite_forbidden_not_owner(client, make_school, make_staff, make_class):
    school = make_school(code="INV-2")
    make_staff(school, email="t@oakwood.edu", password="Password123")
    klass = make_class(school, name="3B")
    signin = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "Password123"})
    token = signin.json()["access_token"]
    r = client.post(
        _invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    assert r.status_code == 403


def test_invite_class_not_found(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    import uuid

    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    r = client.post(
        _invite_url(uuid.uuid4()), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_invite_duplicate_pending_is_409(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    r = client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    assert r.status_code == 409


def test_preview_shows_real_class_and_inviter(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])

    r = client.get(f"/api/v1/invitations/{invite_token}")
    assert r.status_code == 200
    body = r.json()
    assert body["class_name"] == "3A"
    assert body["inviter_email"] == "owner@oakwood.edu"


def test_preview_invalid_token_is_400(client):
    r = client.get("/api/v1/invitations/not-a-real-token")
    assert r.status_code == 400


def test_accept_new_colleague_creates_support_account(
    client, monkeypatch, db, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])

    r = client.post(ACCEPT_URL, json={"token": invite_token, "password": "ColleaguePass1"})
    assert r.status_code == 200
    body = r.json()
    assert body["school_id"] == str(school.id)
    assert body["class_id"] == str(klass.id)

    signin = client.post(SIGNIN, json={"email": "colleague@oakwood.edu", "password": "ColleaguePass1"})
    assert signin.status_code == 200


def test_accept_requires_password_for_new_account(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])
    r = client.post(ACCEPT_URL, json={"token": invite_token})
    assert r.status_code == 422


def test_accept_invalid_token_is_400(client):
    r = client.post(ACCEPT_URL, json={"token": "not-a-real-token", "password": "Whatever123"})
    assert r.status_code == 400


def test_accept_twice_is_rejected(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])
    first = client.post(ACCEPT_URL, json={"token": invite_token, "password": "ColleaguePass1"})
    assert first.status_code == 200
    second = client.post(ACCEPT_URL, json={"token": invite_token, "password": "ColleaguePass1"})
    assert second.status_code == 400  # the accepted token no longer resolves as pending


def test_accepted_colleague_reaches_only_shared_class(
    client, monkeypatch, db, make_school, make_staff, make_class, grant_class_access,
    make_student, add_to_class,
):
    """NEG — GATE G-4: an invited colleague reaches ONLY the shared class and its students, not
    the whole school (a second, un-shared class' student is denied; the whole-school leadership
    surface is denied outright since the granted role is StaffRole.support)."""
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    shared_student = make_student(school, name="Amy")
    add_to_class(klass, shared_student)

    other_klass = make_class(school, name="4B")
    other_student = make_student(school, name="Ben")
    add_to_class(other_klass, other_student)

    client.post(_invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])
    accepted = client.post(ACCEPT_URL, json={"token": invite_token, "password": "ColleaguePass1"})
    assert accepted.status_code == 200

    signin = client.post(SIGNIN, json={"email": "colleague@oakwood.edu", "password": "ColleaguePass1"})
    colleague_token = signin.json()["access_token"]

    # the shared class' student IS reachable
    ok = client.get(f"/api/v1/students/{shared_student.id}", headers=_auth(colleague_token))
    assert ok.status_code == 200

    # a DIFFERENT class' student in the SAME school is NOT reachable
    denied = client.get(f"/api/v1/students/{other_student.id}", headers=_auth(colleague_token))
    assert denied.status_code == 403

    # never whole-school leadership access
    whole_school = client.get(f"/api/v1/schools/{school.id}/staff", headers=_auth(colleague_token))
    assert whole_school.status_code == 403

    access = db.query(StaffClassAccess).filter_by(class_id=klass.id).all()
    support_rows = [a for a in access if a.scope == StaffClassScope.shared]
    assert len(support_rows) == 1


def test_accept_existing_active_colleague_just_grants_access(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    existing = make_staff(school, email="existing@oakwood.edu", password="ExistingPass1",
                           role=StaffRole.teacher)
    client.post(_invite_url(klass.id), json={"email": "existing@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["existing@oakwood.edu"])

    r = client.post(ACCEPT_URL, json={"token": invite_token})  # no password needed
    assert r.status_code == 200
    assert existing.role == StaffRole.teacher  # role is NOT downgraded/changed


def test_resend_supersedes_old_token(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    invite = client.post(
        _invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    invitation_id = invite.json()["invitation_id"]
    old_token = _accept_link_token(captured["colleague@oakwood.edu"])

    resend = client.post(_action_url(invitation_id), json={"action": "resend"}, headers=_auth(token))
    assert resend.status_code == 200
    assert resend.json()["status"] == "sent"
    new_token = _accept_link_token(captured["colleague@oakwood.edu"])
    assert new_token != old_token

    dead = client.post(ACCEPT_URL, json={"token": old_token, "password": "ColleaguePass1"})
    assert dead.status_code == 400

    alive = client.post(ACCEPT_URL, json={"token": new_token, "password": "ColleaguePass1"})
    assert alive.status_code == 200


def test_revoke_kills_the_token(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    invite = client.post(
        _invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    invitation_id = invite.json()["invitation_id"]
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])

    revoke = client.post(_action_url(invitation_id), json={"action": "revoke"}, headers=_auth(token))
    assert revoke.status_code == 200
    assert revoke.json()["status"] == "revoked"

    dead = client.post(ACCEPT_URL, json={"token": invite_token, "password": "ColleaguePass1"})
    assert dead.status_code == 400


def test_action_on_accepted_409(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    invite = client.post(
        _invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    invitation_id = invite.json()["invitation_id"]
    invite_token = _accept_link_token(captured["colleague@oakwood.edu"])
    client.post(ACCEPT_URL, json={"token": invite_token, "password": "ColleaguePass1"})

    r = client.post(_action_url(invitation_id), json={"action": "revoke"}, headers=_auth(token))
    assert r.status_code == 409


def test_action_forbidden_not_owner(
    client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    invite = client.post(
        _invite_url(klass.id), json={"email": "colleague@oakwood.edu"}, headers=_auth(token)
    )
    invitation_id = invite.json()["invitation_id"]

    make_staff(school, email="stranger@oakwood.edu", password="Password123")
    signin = client.post(SIGNIN, json={"email": "stranger@oakwood.edu", "password": "Password123"})
    stranger_token = signin.json()["access_token"]
    r = client.post(
        _action_url(invitation_id), json={"action": "revoke"}, headers=_auth(stranger_token)
    )
    assert r.status_code == 403


def test_deactivated_existing_account_cannot_accept(
    client, monkeypatch, db, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    existing = make_staff(school, email="gone@oakwood.edu", password="Password123")
    existing.status = StaffStatus.deactivated
    db.commit()
    client.post(_invite_url(klass.id), json={"email": "gone@oakwood.edu"}, headers=_auth(token))
    invite_token = _accept_link_token(captured["gone@oakwood.edu"])

    r = client.post(ACCEPT_URL, json={"token": invite_token})
    assert r.status_code == 403
