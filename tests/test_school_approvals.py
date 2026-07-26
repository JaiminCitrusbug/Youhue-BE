"""District/Trust leadership approves or rejects a pending school (FR-02-02).

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  Approve -> school goes LIVE (`active`) and the whole-school Premium trial is ARMED
        (Subscription.state=trial, tier=premium) but NOT started (trial_start_at stays NULL) —
        FR-17-03 starts the clock later, from the first student check-in.
        -> ..._approve_makes_the_school_live_and_arms_the_trial_unstarted,
           ..._approve_activates_the_registrant_together_with_the_school,
           ..._approved_registrant_can_now_sign_in
  MN-2  Reject -> recorded, the school stays NOT live, no trial is armed.
        -> ..._reject_records_the_decision_and_the_school_stays_not_live,
           ..._reject_does_not_activate_the_registrant
  MN-3  Only District/Trust leadership may decide (403 otherwise).
        -> ..._non_district_roles_are_forbidden (teacher, leadership)
  MN-4  404 unknown school; 409 a non-pending school (idempotency/re-decision guard [BR-05]).
        -> ..._unknown_school_is_404, ..._deciding_an_already_active_school_is_409,
           ..._deciding_an_already_rejected_school_is_409, ..._a_second_approve_does_not_double_arm
  GATE G-1 (cross-check, from US-02-01 S2): a pending school still cannot run student check-ins,
        and approval via THIS ticket's endpoint is what lifts the block — proven end-to-end here,
        not re-implemented.
        -> ..._gate_g1_end_to_end_pending_blocks_check_in_then_this_endpoints_approval_unblocks_it

Plus the two read endpoints the FE needs (list + detail — not named in the ticket's BE section but
required so SC-069/SC-070 have real data; logged in the module under review, not invented here).
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import (
    SchoolStatus,
    SessionKind,
    StaffRole,
    StaffStatus,
    SubscriptionState,
    SubscriptionTier,
)
from src.domain.billing.models import Subscription
from src.domain.compliance.models import AuditLog
from src.domain.identity.models import School, StaffAccount

REGISTER = "/api/v1/schools"
SIGNIN = "/api/v1/auth/staff/sign-in"
STUDENT_SIGNIN = "/api/v1/auth/student/sign-in"
PENDING = "/api/v1/schools/pending"


def _decision_url(school_id: object) -> str:
    return f"/api/v1/schools/{school_id}/decision"


def _detail_url(school_id: object) -> str:
    return f"/api/v1/schools/{school_id}"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    """Issue a full (non-MFA-pending) staff session directly, bypassing sign-in. District (and
    leadership) force email-OTP MFA at sign-in (settings.mfa_required_roles) — that flow is already
    covered by INFRA-03 (tests/test_authz.py) and is not this ticket's concern; this ticket only
    needs an authenticated actor of the right ROLE reaching the endpoint."""
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _district_token(db, make_school, make_staff, *, code: str = "DIST-1") -> str:
    """A District/Trust leadership actor — belongs to their OWN already-active school (the schema
    has no Trust/District-to-school mapping; see the review note in
    application/schools/services.py)."""
    home = make_school(code=code, status=SchoolStatus.active, name=f"Home {code}")
    staff = make_staff(home, email=f"district-{code}@trust.example", role=StaffRole.district)
    return _mint(db, staff)


# --- MN-1 / Scenario 1: approve arms the trial without starting it ------------------------------
def test_approve_makes_the_school_live_and_arms_the_trial_unstarted(
    client, db, make_school, make_staff
):
    pending = make_school(code="PEND-1", status=SchoolStatus.pending, name="Oakwood Primary")
    token = _district_token(db, make_school, make_staff)

    r = client.post(_decision_url(pending.id), json={"decision": "approve"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"school_id": str(pending.id), "status": "active"}

    db.refresh(pending)
    assert pending.status == SchoolStatus.active

    sub = db.query(Subscription).filter(Subscription.school_id == pending.id).one()
    assert sub.tier == SubscriptionTier.premium
    assert sub.state == SubscriptionState.trial
    assert sub.trial_start_at is None  # ARMED, not started — FR-17-03 starts it at first check-in
    assert sub.trial_end_at is None


def test_approve_activates_the_registrant_together_with_the_school(
    client, db, make_school, make_staff
):
    """FR-02-01 documents this explicitly: the self-registrant is left `invited` until FR-02-02
    activates the school and its registrant TOGETHER — this is where that happens."""
    reg = client.post(REGISTER, json={
        "school_name": "Greenfield High", "registrant_email": "head@greenfield.edu",
        "password": "Password123",
    })
    school_id = reg.json()["school_id"]
    staff = db.query(StaffAccount).filter(StaffAccount.school_id == school_id).one()
    assert staff.status == StaffStatus.invited

    token = _district_token(db, make_school, make_staff)
    r = client.post(_decision_url(school_id), json={"decision": "approve"}, headers=_auth(token))
    assert r.status_code == 200

    db.refresh(staff)
    assert staff.status == StaffStatus.active


def test_approve_walks_the_registrant_through_the_real_lifecycle_not_a_direct_jump(
    client, db, make_school, make_staff, monkeypatch
):
    """FR-02-04 review fix (Major): district approval used to set `staff.status = active` directly,
    bypassing the shared state machine entirely — a real gap in FR-02-04's own core claim ("one
    state machine, accurate lifecycle everywhere"). Now it must route through
    `staff_lifecycle.advance_to`, the SAME mechanism `invitations.services.accept_invitation` uses,
    so a tightened `_LEGAL_TRANSITIONS` graph would still be honoured on this onboarding path too."""
    import src.application.schools.services as schools_svc
    from src.application.staff_lifecycle import services as staff_lifecycle

    calls: list[tuple[object, object]] = []
    real_advance_to = staff_lifecycle.advance_to

    def _spy(db_, staff_, target):
        calls.append((staff_.id, target))
        return real_advance_to(db_, staff_, target)

    monkeypatch.setattr(schools_svc.staff_lifecycle, "advance_to", _spy)

    reg = client.post(REGISTER, json={
        "school_name": "Ashford Prep", "registrant_email": "head@ashford.edu",
        "password": "Password123",
    })
    school_id = reg.json()["school_id"]
    staff = db.query(StaffAccount).filter(StaffAccount.school_id == school_id).one()
    assert staff.status == StaffStatus.invited

    token = _district_token(db, make_school, make_staff)
    r = client.post(_decision_url(school_id), json={"decision": "approve"}, headers=_auth(token))
    assert r.status_code == 200

    assert calls == [(staff.id, StaffStatus.active)]  # routed through the real state machine
    db.refresh(staff)
    assert staff.status == StaffStatus.active


def test_approved_registrant_can_now_sign_in(client, db, make_school, make_staff):
    reg = client.post(REGISTER, json={
        "school_name": "Birchwood Academy", "registrant_email": "head@birchwood.edu",
        "password": "Password123",
    })
    school_id = reg.json()["school_id"]
    token = _district_token(db, make_school, make_staff)
    client.post(_decision_url(school_id), json={"decision": "approve"}, headers=_auth(token))

    r = client.post(SIGNIN, json={"email": "head@birchwood.edu", "password": "Password123"})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_approve_writes_an_audit_log_row(client, db, make_school, make_staff):
    pending = make_school(code="PEND-A", status=SchoolStatus.pending)
    token = _district_token(db, make_school, make_staff)
    client.post(_decision_url(pending.id), json={"decision": "approve"}, headers=_auth(token))

    row = db.query(AuditLog).filter(AuditLog.action == "school.approved").one()
    assert row.school_id == pending.id


# --- MN-2 / Scenario 2: reject records the decision, no trial armed -----------------------------
def test_reject_records_the_decision_and_the_school_stays_not_live(
    client, db, make_school, make_staff
):
    pending = make_school(code="PEND-2", status=SchoolStatus.pending, name="Elmwood Primary")
    token = _district_token(db, make_school, make_staff)

    r = client.post(_decision_url(pending.id), json={"decision": "reject"}, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {"school_id": str(pending.id), "status": "rejected"}

    db.refresh(pending)
    assert pending.status == SchoolStatus.rejected
    assert db.query(Subscription).filter(Subscription.school_id == pending.id).count() == 0


def test_reject_does_not_activate_the_registrant(client, db, make_school, make_staff):
    reg = client.post(REGISTER, json={
        "school_name": "Foxglove Primary", "registrant_email": "head@foxglove.edu",
        "password": "Password123",
    })
    school_id = reg.json()["school_id"]
    staff = db.query(StaffAccount).filter(StaffAccount.school_id == school_id).one()

    token = _district_token(db, make_school, make_staff)
    client.post(_decision_url(school_id), json={"decision": "reject"}, headers=_auth(token))

    db.refresh(staff)
    assert staff.status == StaffStatus.invited
    refused = client.post(SIGNIN, json={"email": "head@foxglove.edu", "password": "Password123"})
    assert refused.status_code == 401


def test_reject_writes_an_audit_log_row(client, db, make_school, make_staff):
    pending = make_school(code="PEND-R", status=SchoolStatus.pending)
    token = _district_token(db, make_school, make_staff)
    client.post(_decision_url(pending.id), json={"decision": "reject"}, headers=_auth(token))

    row = db.query(AuditLog).filter(AuditLog.action == "school.rejected").one()
    assert row.school_id == pending.id


def test_a_rejected_schools_name_is_free_again(client, db, make_school, make_staff):
    """Confirms rejection composes with FR-02-01's `uq_schools_name_live` exclusion — a rejected
    school's name is not a permanent dead end."""
    pending = make_school(code="PEND-3", status=SchoolStatus.pending, name="Somewhere Primary")
    token = _district_token(db, make_school, make_staff)
    client.post(_decision_url(pending.id), json={"decision": "reject"}, headers=_auth(token))

    r = client.post(REGISTER, json={
        "school_name": "Somewhere Primary", "registrant_email": "new@somewhere.edu",
        "password": "Password123",
    })
    assert r.status_code == 201


# --- MN-3: only District/Trust leadership may decide (403) --------------------------------------
@pytest.mark.authz
@pytest.mark.parametrize("role", [StaffRole.teacher, StaffRole.leadership, StaffRole.support])
def test_non_district_roles_are_forbidden(client, db, make_school, make_staff, role):
    pending = make_school(code="PEND-4", status=SchoolStatus.pending)
    actor_school = make_school(code="ACTOR-1", status=SchoolStatus.active)
    actor = make_staff(actor_school, email=f"actor-{role.value}@school.edu", role=role)
    token = _mint(db, actor)

    r = client.post(_decision_url(pending.id), json={"decision": "approve"}, headers=_auth(token))
    assert r.status_code == 403

    db.refresh(pending)
    assert pending.status == SchoolStatus.pending  # nothing changed


def test_forbidden_read_endpoints_for_non_district_roles(client, db, make_school, make_staff):
    make_school(code="PEND-5", status=SchoolStatus.pending)
    actor_school = make_school(code="ACTOR-2", status=SchoolStatus.active)
    actor = make_staff(actor_school, email="teacher@school.edu", role=StaffRole.teacher)
    token = _mint(db, actor)

    assert client.get(PENDING, headers=_auth(token)).status_code == 403


def test_unauthenticated_decision_is_refused(client, make_school):
    pending = make_school(code="PEND-6", status=SchoolStatus.pending)
    r = client.post(_decision_url(pending.id), json={"decision": "approve"})
    assert r.status_code in (401, 403)  # no bearer token at all


# --- MN-4: 404 unknown school; 409 not pending (idempotency / re-decision guard) -----------------
def test_unknown_school_is_404(client, db, make_school, make_staff):
    token = _district_token(db, make_school, make_staff)
    r = client.post(
        _decision_url(uuid.uuid4()), json={"decision": "approve"}, headers=_auth(token)
    )
    assert r.status_code == 404


def test_deciding_an_already_active_school_is_409(client, db, make_school, make_staff):
    live = make_school(code="LIVE-9", status=SchoolStatus.active)
    token = _district_token(db, make_school, make_staff)

    r = client.post(_decision_url(live.id), json={"decision": "approve"}, headers=_auth(token))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_pending"


def test_deciding_an_already_rejected_school_is_409(client, db, make_school, make_staff):
    rejected = make_school(code="REJ-9", status=SchoolStatus.rejected)
    token = _district_token(db, make_school, make_staff)

    r = client.post(_decision_url(rejected.id), json={"decision": "reject"}, headers=_auth(token))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "not_pending"


def test_a_second_approve_does_not_double_arm_the_trial(client, db, make_school, make_staff):
    pending = make_school(code="PEND-7", status=SchoolStatus.pending)
    token = _district_token(db, make_school, make_staff)

    first = client.post(_decision_url(pending.id), json={"decision": "approve"}, headers=_auth(token))
    assert first.status_code == 200
    second = client.post(_decision_url(pending.id), json={"decision": "approve"}, headers=_auth(token))
    assert second.status_code == 409

    assert db.query(Subscription).filter(Subscription.school_id == pending.id).count() == 1


# --- GATE G-1 (cross-check): pending blocks check-in; THIS endpoint's approval unblocks it -------
def test_gate_g1_end_to_end_pending_blocks_check_in_then_approval_unblocks_it(
    client, db, make_school, make_staff, make_student
):
    reg = client.post(REGISTER, json={
        "school_name": "Hollybush Primary", "registrant_email": "head@hollybush.edu",
        "password": "Password123",
    })
    school_id = reg.json()["school_id"]
    school = db.get(School, school_id)
    student = make_student(school, name="Sam")

    blocked = client.post(
        STUDENT_SIGNIN,
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    assert blocked.status_code == 400  # not live -> the code does not resolve (GATE G-1)

    token = _district_token(db, make_school, make_staff)
    approved = client.post(
        _decision_url(school_id), json={"decision": "approve"}, headers=_auth(token)
    )
    assert approved.status_code == 200

    allowed = client.post(
        STUDENT_SIGNIN,
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    assert allowed.status_code == 200
    assert allowed.json()["session_token"]


# --- Validation: unknown decision value / extra fields -------------------------------------------
def test_invalid_decision_value_is_rejected(client, db, make_school, make_staff):
    pending = make_school(code="PEND-8", status=SchoolStatus.pending)
    token = _district_token(db, make_school, make_staff)
    r = client.post(
        _decision_url(pending.id), json={"decision": "maybe"}, headers=_auth(token)
    )
    assert r.status_code == 422


def test_unknown_body_fields_are_rejected(client, db, make_school, make_staff):
    pending = make_school(code="PEND-8B", status=SchoolStatus.pending)
    token = _district_token(db, make_school, make_staff)
    r = client.post(
        _decision_url(pending.id),
        json={"decision": "approve", "status": "active"},
        headers=_auth(token),
    )
    assert r.status_code == 422


# --- FE read surface: GET /schools/pending + GET /schools/{id} -----------------------------------
def test_pending_list_is_oldest_first_and_district_gated(client, db, make_school, make_staff):
    older = make_school(code="OLD-1", status=SchoolStatus.pending, name="Old School")
    older.created_at = datetime.now(UTC) - timedelta(days=2)
    newer = make_school(code="NEW-1", status=SchoolStatus.pending, name="New School")
    newer.created_at = datetime.now(UTC) - timedelta(hours=1)
    live = make_school(code="LIVE-8", status=SchoolStatus.active)  # must not appear
    db.commit()

    token = _district_token(db, make_school, make_staff)
    r = client.get(PENDING, headers=_auth(token))
    assert r.status_code == 200
    ids = [row["school_id"] for row in r.json()["schools"]]
    assert ids == [str(older.id), str(newer.id)]
    assert str(live.id) not in ids


def test_pending_list_includes_the_registrant_email(client, db, make_school, make_staff):
    reg = client.post(REGISTER, json={
        "school_name": "Thistledown Primary", "registrant_email": "head@thistledown.edu",
        "password": "Password123",
    })
    school_id = reg.json()["school_id"]

    token = _district_token(db, make_school, make_staff)
    r = client.get(PENDING, headers=_auth(token))
    row = next(row for row in r.json()["schools"] if row["school_id"] == school_id)
    assert row["registrant_email"] == "head@thistledown.edu"


def test_school_detail_returns_status_registrant_and_student_count(
    client, db, make_school, make_staff, make_student
):
    school = make_school(code="DET-1", status=SchoolStatus.pending, name="Detail Primary")
    make_staff(school, email="head@detail.edu", role=StaffRole.teacher)
    make_student(school, name="A")
    make_student(school, name="B")

    token = _district_token(db, make_school, make_staff)
    r = client.get(_detail_url(school.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "pending"
    assert body["registrant_email"] == "head@detail.edu"
    assert body["student_count"] == 2


def test_school_detail_unknown_school_is_404(client, db, make_school, make_staff):
    token = _district_token(db, make_school, make_staff)
    r = client.get(_detail_url(uuid.uuid4()), headers=_auth(token))
    assert r.status_code == 404


def test_school_detail_forbidden_for_non_district_role(client, db, make_school, make_staff):
    pending = make_school(code="DET-2", status=SchoolStatus.pending)
    actor_school = make_school(code="ACTOR-3", status=SchoolStatus.active)
    actor = make_staff(actor_school, email="teacher2@school.edu", role=StaffRole.teacher)
    token = _mint(db, actor)

    r = client.get(_detail_url(pending.id), headers=_auth(token))
    assert r.status_code == 403


def test_decide_school_unexpected_failure_rolls_back(client, db, make_school, make_staff, monkeypatch):
    """The last-resort 500 guard: fail AFTER the status flip has been staged in-session, so a
    missing rollback would leave the school half-approved (live but no armed trial)."""
    import src.application.schools.services as schools_svc

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(schools_svc.identity_db, "list_invited_staff_in_school", _boom)

    pending = make_school(code="PEND-BOOM", status=SchoolStatus.pending)
    token = _district_token(db, make_school, make_staff)
    r = client.post(_decision_url(pending.id), json={"decision": "approve"}, headers=_auth(token))

    assert r.status_code == 500
    assert r.json()["detail"] == "Decision failed"
    db.refresh(pending)
    assert pending.status == SchoolStatus.pending  # rolled back — never left half-approved
    assert db.query(Subscription).filter(Subscription.school_id == pending.id).count() == 0


# --- OpenAPI: the frontend generates its client from this ---------------------------------------
def test_openapi_documents_the_decision_endpoint_statuses(client):
    responses = client.get("/openapi.json").json()["paths"]["/api/v1/schools/{school_id}/decision"]["post"]["responses"]
    assert {"200", "403", "404", "409", "422", "500"} <= set(responses)
    conflict = responses["409"]["content"]["application/json"]["schema"]
    assert conflict["$ref"].endswith("DecisionConflictResponse")
