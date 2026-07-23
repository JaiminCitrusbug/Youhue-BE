"""School self-registration (FR-02-01) — pending create, no-duplicate join, server-side validation.

Covers the ticket's three scenarios + the validation scenario + retry idempotency:
  1. A teacher self-registers → school created PENDING (201 {school_id, status: 'pending'}).
  2. A pending school is not live → its code does not resolve for student sign-in (GATE G-1).
  3. A later teacher whose school name is already APPROVED is routed to join it (409), not duplicated.
  +  server-side validation (empty name / bad email → 422) and idempotent retry.
"""
from src.constants.enums import SchoolStatus, SchoolTier, StaffRole, StaffStatus
from src.domain.identity import services as identity_db
from src.domain.identity.models import School, StaffAccount

REGISTER = "/api/v1/schools"


def _payload(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "school_name": "Oakwood Primary",
        "registrant_email": "head@oakwood.edu",
        "password": "Password123",
    }
    body.update(over)
    return body


# --- Scenario 1: register a new school in a pending state -------------------------------------
def test_register_creates_pending_school(client, db):
    r = client.post(REGISTER, json=_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"
    school_id = body["school_id"]

    school = db.get(School, school_id)
    assert school is not None
    assert school.status == SchoolStatus.pending
    assert school.tier == SchoolTier.free
    assert school.sign_in_code  # a unique school sign-in code was allocated


def test_register_associates_registrant_as_active_teacher(client, db):
    r = client.post(REGISTER, json=_payload(registrant_email="Head@Oakwood.edu"))
    school_id = r.json()["school_id"]
    staff = db.query(StaffAccount).filter(StaffAccount.school_id == school_id).all()
    assert len(staff) == 1
    assert staff[0].email == "head@oakwood.edu"  # normalised lower-case
    assert staff[0].role == StaffRole.teacher
    assert staff[0].status == StaffStatus.active
    assert staff[0].password_hash  # password hashed, never stored plaintext


def test_registrant_can_sign_in_after_registering(client):
    assert client.post(REGISTER, json=_payload()).status_code == 201
    r = client.post(
        "/api/v1/auth/staff/sign-in",
        json={"email": "head@oakwood.edu", "password": "Password123"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


# --- Scenario 2: a pending school is not live (GATE G-1) --------------------------------------
def test_pending_school_code_does_not_resolve_as_live(client, db):
    r = client.post(REGISTER, json=_payload())
    school = db.get(School, r.json()["school_id"])
    # A pending school is not live → its sign-in code is not resolvable for student check-in.
    assert identity_db.get_active_school_by_code(db, school.sign_in_code) is None


# --- Scenario 3: a later teacher joins the existing approved school (no duplicate) ------------
def test_later_teacher_of_approved_school_gets_join_conflict(client, db, make_school):
    approved = make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    r = client.post(REGISTER, json=_payload(registrant_email="new.teacher@oakwood.edu"))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["action"] == "join"
    assert detail["school_id"] == str(approved.id)
    # no duplicate school was created
    assert db.query(School).filter(School.name == "Oakwood Primary").count() == 1


def test_approved_name_match_is_case_insensitive(client, db, make_school):
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    r = client.post(REGISTER, json=_payload(school_name="  oakwood primary  "))
    assert r.status_code == 409
    assert r.json()["detail"]["action"] == "join"


# --- already-member: registrant already owns an approved school → forbidden -------------------
def test_existing_member_of_approved_school_is_forbidden(client, make_school, make_staff):
    school = make_school(code="OAK-LIVE", status=SchoolStatus.active)
    make_staff(school, email="head@oakwood.edu", password="Password123")
    r = client.post(REGISTER, json=_payload())
    assert r.status_code == 403
    assert r.json()["detail"]["school_id"] == str(school.id)


# --- idempotency on retry: re-submitting a pending registration is not a duplicate ------------
def test_retry_of_pending_registration_is_idempotent(client, db):
    first = client.post(REGISTER, json=_payload())
    second = client.post(REGISTER, json=_payload())
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["school_id"] == second.json()["school_id"]
    assert db.query(School).filter(School.name == "Oakwood Primary").count() == 1


# --- a pending (not-yet-approved) name is NOT a join target — only approved schools are -------
def test_second_teacher_of_pending_name_creates_new_school(client, db):
    client.post(REGISTER, json=_payload())
    r = client.post(REGISTER, json=_payload(registrant_email="other@oakwood.edu"))
    assert r.status_code == 201  # only an APPROVED same-name school routes to join
    assert db.query(School).filter(School.name == "Oakwood Primary").count() == 2


# --- server-side validation (Scenario: inputs validated server-side) --------------------------
def test_empty_school_name_rejected(client):
    assert client.post(REGISTER, json=_payload(school_name="")).status_code == 422


def test_whitespace_only_school_name_rejected(client):
    assert client.post(REGISTER, json=_payload(school_name="   ")).status_code == 422


def test_invalid_email_rejected(client):
    assert client.post(REGISTER, json=_payload(registrant_email="not-an-email")).status_code == 422


def test_short_password_rejected(client):
    assert client.post(REGISTER, json=_payload(password="short")).status_code == 422
