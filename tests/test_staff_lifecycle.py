"""FR-02-04 — staff account lifecycle accuracy.

  Scenario 1  A staff account moves through invited -> sent -> accepted -> active -> deactivated
              in turn (never a silent jump).                -> test_new_invitee_walks_full_lifecycle
  Scenario 2  (NEG) An invited-but-not-accepted account never reads as active.
              -> test_invited_account_not_shown_as_active
  Scenario 3  (NEG) A superseded (resent) invitation link gets a SPECIFIC message, distinct from a
              revoked or expired one, not one generic bucket.
              -> test_resend_gives_specific_superseded_message
              -> test_revoke_gives_specific_revoked_message
  PATCH /api/v1/staff/{id}/status: legal hop succeeds, illegal hop 409, cross-school/non-leadership
  403/404, unknown id 404.         -> test_status_endpoint_*
  Concurrency: a `deactivate` racing an `activate`-from-the-pre-deactivate-state must serialize —
  the loser sees the post-commit `deactivated` state and gets a real 409, never a silent reactivation.
              -> test_concurrent_deactivate_and_activate_never_reactivates
"""
import threading
import uuid

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.application.invitations.services as invitations_mod
from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.constants.enums import SessionKind, StaffClassScope, StaffRole, StaffStatus
from src.domain.identity.models import StaffAccount

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _signin(client, email: str, password: str = "Password123") -> str:
    r = client.post(SIGNIN, json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _mint(db, staff: StaffAccount) -> str:
    """Leadership forces email-OTP MFA at sign-in (settings.mfa_roles) — that flow is INFRA-03's
    own concern (tests/test_authz.py). Mirrors test_leadership.py's own `_mint` precedent: issue a
    full (non-MFA-pending) session directly for a leadership actor under test here."""
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _accept_link_token(body: str) -> str:
    return body.split("token=")[-1].strip().rstrip(")")


def _owner_signed_in(db, client, monkeypatch, make_school, make_staff, make_class, grant_class_access):
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        invitations_mod, "send_email", lambda to, subject, body: captured.__setitem__(to, body)
    )
    school = make_school(code="LIFE-1")
    owner = make_staff(school, email="owner@lifecycle.edu", password="Password123",
                        role=StaffRole.leadership)
    klass = make_class(school, name="3A")
    grant_class_access(owner, klass, scope=StaffClassScope.owner)
    token = _mint(db, owner)
    return school, owner, klass, token, captured


def test_new_invitee_walks_full_lifecycle(
    client, db, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    """Scenario 1: a brand-new invitee has NO staff row until they accept — the account is then
    created and lands at `active`, having walked invited -> sent -> accepted -> active within the
    accept transaction (never default-constructed straight at active)."""
    school, owner, klass, token, captured = _owner_signed_in(
        db, client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    client.post(
        f"/api/v1/classes/{klass.id}/invitations", json={"email": "new@lifecycle.edu"},
        headers=_auth(token),
    )
    invite_token = _accept_link_token(captured["new@lifecycle.edu"])

    r = client.post(
        "/api/v1/invitations/accept", json={"token": invite_token, "password": "ColleaguePass1"}
    )
    assert r.status_code == 200, r.text

    staff = db.query(StaffAccount).filter(StaffAccount.email == "new@lifecycle.edu").one()
    assert staff.status == StaffStatus.active
    assert staff.role == StaffRole.support

    # Deactivate via the leadership endpoint (FR-16-02) — final leg of the named lifecycle,
    # now backed by the same shared state machine (`staff_lifecycle.transition`).
    d = client.patch(
        f"/api/v1/schools/{school.id}/staff/{staff.id}", json={"status": "deactivated"},
        headers=_auth(token),
    )
    assert d.status_code == 200, d.text
    db.refresh(staff)
    assert staff.status == StaffStatus.deactivated


def test_invited_account_not_shown_as_active(client, db, make_school, make_staff):
    """Scenario 2 (NEG): an account sitting at `invited` (FR-02-01's registrant, before FR-02-02
    approval) must render its TRUE status on the staff list — never as active."""
    school = make_school(code="LIFE-2")
    leader = make_staff(school, email="leader@lifecycle.edu", role=StaffRole.leadership)
    registrant = StaffAccount(
        school_id=school.id, email="pending@lifecycle.edu", password_hash="x",
        role=StaffRole.leadership, status=StaffStatus.invited,
    )
    db.add(registrant)
    db.commit()

    token = _mint(db, leader)
    r = client.get(f"/api/v1/schools/{school.id}/staff", headers=_auth(token))
    assert r.status_code == 200
    row = next(s for s in r.json()["staff"] if s["email"] == "pending@lifecycle.edu")
    assert row["status"] == "invited"  # never "active"


def test_resend_gives_specific_superseded_message(
    db, client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    """Scenario 3 (NEG): the OLD link, after a resend, reports it was superseded — a message
    distinct from the generic "invalid" bucket a never-existed token gets."""
    school, owner, klass, token, captured = _owner_signed_in(
        db, client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    invite = client.post(
        f"/api/v1/classes/{klass.id}/invitations", json={"email": "stale@lifecycle.edu"},
        headers=_auth(token),
    )
    invitation_id = invite.json()["invitation_id"]
    old_token = _accept_link_token(captured["stale@lifecycle.edu"])

    resend = client.post(
        f"/api/v1/invitations/{invitation_id}/action", json={"action": "resend"},
        headers=_auth(token),
    )
    assert resend.status_code == 200

    dead = client.post(
        "/api/v1/invitations/accept", json={"token": old_token, "password": "ColleaguePass1"}
    )
    assert dead.status_code == 400
    assert "newer invitation" in dead.json()["detail"]

    garbage = client.post(
        "/api/v1/invitations/accept",
        json={"token": "not-a-real-token-at-all", "password": "ColleaguePass1"},
    )
    assert garbage.status_code == 400
    assert "newer invitation" not in garbage.json()["detail"]  # generic, not the specific reason


def test_revoke_gives_specific_revoked_message(
    db, client, monkeypatch, make_school, make_staff, make_class, grant_class_access
):
    school, owner, klass, token, captured = _owner_signed_in(
        db, client, monkeypatch, make_school, make_staff, make_class, grant_class_access
    )
    invite = client.post(
        f"/api/v1/classes/{klass.id}/invitations", json={"email": "revoked@lifecycle.edu"},
        headers=_auth(token),
    )
    invitation_id = invite.json()["invitation_id"]
    invite_token = _accept_link_token(captured["revoked@lifecycle.edu"])

    client.post(
        f"/api/v1/invitations/{invitation_id}/action", json={"action": "revoke"},
        headers=_auth(token),
    )
    dead = client.post(
        "/api/v1/invitations/accept", json={"token": invite_token, "password": "ColleaguePass1"}
    )
    assert dead.status_code == 400
    assert "revoked" in dead.json()["detail"]


def test_status_endpoint_legal_transition(client, db, make_school, make_staff):
    school = make_school(code="LIFE-5")
    leader = make_staff(school, email="leader5@lifecycle.edu", role=StaffRole.leadership)
    target = StaffAccount(
        school_id=school.id, email="target5@lifecycle.edu", password_hash="x",
        role=StaffRole.teacher, status=StaffStatus.invited,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    token = _mint(db, leader)
    r = client.patch(
        f"/api/v1/staff/{target.id}/status", json={"status": "sent"}, headers=_auth(token)
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent"


def test_status_endpoint_illegal_transition_409(client, db, make_school, make_staff):
    school = make_school(code="LIFE-6")
    leader = make_staff(school, email="leader6@lifecycle.edu", role=StaffRole.leadership)
    target = StaffAccount(
        school_id=school.id, email="target6@lifecycle.edu", password_hash="x",
        role=StaffRole.teacher, status=StaffStatus.deactivated,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    token = _mint(db, leader)
    r = client.patch(
        f"/api/v1/staff/{target.id}/status", json={"status": "active"}, headers=_auth(token)
    )
    assert r.status_code == 409


def test_status_endpoint_forbidden_cross_school(client, db, make_school, make_staff):
    school_a = make_school(code="LIFE-7A")
    school_b = make_school(code="LIFE-7B")
    leader = make_staff(school_a, email="leader7@lifecycle.edu", role=StaffRole.leadership)
    other = make_staff(school_b, email="other7@lifecycle.edu", role=StaffRole.teacher)

    token = _mint(db, leader)
    r = client.patch(
        f"/api/v1/staff/{other.id}/status", json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 404  # existence hidden, mirrors leadership.deactivate_staff


def test_status_endpoint_forbidden_not_leadership(client, db, make_school, make_staff):
    school = make_school(code="LIFE-8")
    make_staff(school, email="teacher8@lifecycle.edu", role=StaffRole.teacher)
    target = make_staff(school, email="target8@lifecycle.edu", role=StaffRole.teacher)

    token = _signin(client, "teacher8@lifecycle.edu")
    r = client.patch(
        f"/api/v1/staff/{target.id}/status", json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 403


def test_status_endpoint_not_found(client, db, make_school, make_staff):
    school = make_school(code="LIFE-9")
    leader = make_staff(school, email="leader9@lifecycle.edu", role=StaffRole.leadership)
    token = _mint(db, leader)
    r = client.patch(
        f"/api/v1/staff/{uuid.uuid4()}/status", json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_concurrent_deactivate_and_activate_never_reactivates(db, make_school, make_staff):
    """Real concurrency, real separate DB sessions (matches test_alert_config.py's established
    methodology for this bug class). Without `get_staff_in_school_for_update`'s row lock, the
    `activate` worker could read the pre-deactivate status, then commit `active` AFTER the
    deactivate has already committed — silently reactivating a deactivated account. The lock forces
    the `activate` worker to block until the `deactivate` worker's transaction ends, then re-read
    the CURRENT (post-commit) status — so it correctly sees `deactivated` and gets a real 409."""
    import time

    from src.application.staff_lifecycle import services as staff_lifecycle_svc
    from src.domain.identity import services as identity_db

    school = make_school(code="LIFE-10", name="Lifecycle LIFE-10")
    target = StaffAccount(
        school_id=school.id, email="race@lifecycle.edu", password_hash="x",
        role=StaffRole.teacher, status=StaffStatus.accepted,
    )
    db.add(target)
    db.commit()
    db.refresh(target)

    engine = create_engine(env_settings.database_url_test, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    # Generous timeouts (barrier + join): this suite runs alongside other concurrent lanes on the
    # same box, so a tight timeout here risks a spurious KeyError/failure from a merely-SLOW thread
    # under contention, not an actual correctness violation — the assertions below are what prove
    # correctness, not how fast the threads got there.
    def _deactivate() -> None:
        sess = Session()
        try:
            row = identity_db.get_staff_in_school_for_update(sess, school.id, target.id)
            barrier.wait(timeout=30)  # hold the lock until the activate worker is blocked on it
            time.sleep(0.3)
            staff_lifecycle_svc.transition(sess, row, StaffStatus.deactivated)
            sess.commit()
            results["deactivate"] = "ok"
        except BaseException as exc:  # noqa: BLE001 - captured, asserted on the main thread
            sess.rollback()
            results["deactivate"] = exc
        finally:
            sess.close()

    def _activate() -> None:
        sess = Session()
        try:
            barrier.wait(timeout=30)
            # Blocks here until `_deactivate` commits and releases the row lock, then re-reads the
            # NOW-current (post-commit) status.
            row = identity_db.get_staff_in_school_for_update(sess, school.id, target.id)
            staff_lifecycle_svc.transition(sess, row, StaffStatus.active)
            sess.commit()
            results["activate"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            sess.rollback()
            results["activate"] = exc
        finally:
            sess.close()

    t1 = threading.Thread(target=_deactivate)
    t2 = threading.Thread(target=_activate)
    t1.start()
    t2.start()
    t1.join(timeout=120)
    t2.join(timeout=120)

    # A thread still alive means it genuinely hung (not just "slow") — that's its own real bug, but
    # a distinct one from what this test targets; fail loudly instead of a misleading KeyError.
    assert not t1.is_alive(), "deactivate worker did not finish — investigate separately, not a race result"
    assert not t2.is_alive(), "activate worker did not finish — investigate separately, not a race result"

    assert results["deactivate"] == "ok"
    from fastapi import HTTPException

    assert isinstance(results["activate"], HTTPException)
    assert results["activate"].status_code == 409  # illegal: deactivated is terminal

    db.refresh(target)
    assert target.status == StaffStatus.deactivated  # never silently reactivated
