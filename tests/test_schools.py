"""School self-registration (FR-02-01) — pending create, no-duplicate, non-enumerating, ACID.

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  created PENDING and NOT live until District approval
        -> ..._creates_pending_school, ..._registrant_is_not_active_yet,
           ..._registrant_cannot_sign_in_while_pending, ..._pending_school_session_has_no_effect
  MN-2  a pending school cannot run student check-ins (NEG, GATE G-1)
        -> ..._pending_school_cannot_run_student_check_in  (end-to-end, with a positive control)
  MN-3  a later teacher must NOT create a duplicate school (NEG)
        -> ..._later_teacher_gets_conflict, ..._name_match_ignores_case_and_whitespace,
           ..._duplicate_name_is_refused_by_the_database, ..._lost_race_is_a_conflict_not_a_duplicate
  MN-4  server-side validation is the gate; ACID write with idempotency on retry [BR-05]
        -> the validation block, ..._retry_is_idempotent, ..._replay_needs_the_password,
           ..._conflict_leaves_the_database_unchanged, ..._unexpected_failure_rolls_back
"""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import SchoolStatus, SchoolTier, SessionKind, StaffRole, StaffStatus
from src.domain.auth.models import AuthSession
from src.domain.billing.models import Notification
from src.domain.identity import services as identity_db
from src.domain.identity.models import School, StaffAccount
from src.utils import security

REGISTER = "/api/v1/schools"
SIGNIN = "/api/v1/auth/staff/sign-in"
STUDENT_SIGNIN = "/api/v1/auth/student/sign-in"
NOTIFY = "/api/v1/notifications"


def _payload(**over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "school_name": "Oakwood Primary",
        "registrant_email": "head@oakwood.edu",
        "password": "Password123",
    }
    body.update(over)
    return body


def _staff_of(db, school_id) -> StaffAccount:
    return db.query(StaffAccount).filter(StaffAccount.school_id == school_id).one()


def _mint_staff_session(db, staff: StaffAccount) -> str:
    """Issue a staff session directly, bypassing sign-in — models 'whatever mints a session, a
    pending school still cannot act'."""
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- MN-1 / Scenario 1: register a new school in a pending state ------------------------------
def test_register_creates_pending_school(client, db):
    r = client.post(REGISTER, json=_payload())
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "pending"

    school = db.get(School, body["school_id"])
    assert school is not None
    assert school.status == SchoolStatus.pending
    assert school.tier == SchoolTier.free
    assert school.sign_in_code  # a unique school sign-in code was allocated


def test_registrant_is_associated_but_not_active_yet(client, db):
    """MN-1: the registrant of a pending school is NOT a usable account. If this flips back to
    `active`, an unverified email owns a live staff identity before anyone approved anything."""
    r = client.post(REGISTER, json=_payload(registrant_email="Head@Oakwood.edu"))
    staff = _staff_of(db, r.json()["school_id"])
    assert staff.email == "head@oakwood.edu"  # normalised lower-case
    assert staff.role == StaffRole.teacher
    assert staff.status == StaffStatus.invited
    # ...and the account is therefore invisible to the global sign-in lookup:
    assert identity_db.get_active_staff_by_email(db, "head@oakwood.edu") is None


def test_password_is_hashed_never_stored_in_the_clear(client, db):
    r = client.post(REGISTER, json=_payload())
    staff = _staff_of(db, r.json()["school_id"])
    assert staff.password_hash is not None
    assert staff.password_hash != "Password123"
    assert staff.password_hash.startswith("$2b$")  # bcrypt, not a reversible encoding
    assert security.verify_password("Password123", staff.password_hash) is True
    assert security.verify_password("Password124", staff.password_hash) is False


def test_registrant_cannot_sign_in_while_school_is_pending(client, db):
    """INVERTED from the branch's original `test_registrant_can_sign_in_after_registering`, which
    asserted the vulnerability as a feature: three anonymous calls used to yield a 12-hour non-MFA
    staff session on an unverified email. Registration must mint no session at all."""
    assert client.post(REGISTER, json=_payload()).status_code == 201

    refused = client.post(SIGNIN, json={"email": "head@oakwood.edu", "password": "Password123"})
    ghost = client.post(SIGNIN, json={"email": "nobody@oakwood.edu", "password": "Password123"})
    assert refused.status_code == 401
    assert "access_token" not in refused.json()
    # ...and indistinguishable from an unknown email: registering leaks no account existence.
    assert refused.json() == ghost.json()
    assert db.query(AuthSession).count() == 0  # nothing was issued, not merely not returned


def test_active_staff_of_a_pending_school_is_refused_a_session(client, db, make_school, make_staff):
    """The second, independent half of the guard: even an ACTIVE account gets no session while its
    school is not live (this is the state FR-02-02 produces when it rejects a school)."""
    school = make_school(code="PEND-1", status=SchoolStatus.pending)
    make_staff(school, email="head@pending.edu", password="Password123")

    r = client.post(SIGNIN, json={"email": "head@pending.edu", "password": "Password123"})
    assert r.status_code == 403
    assert db.query(AuthSession).count() == 0


def test_pending_school_session_cannot_reach_a_staff_write_endpoint(
    client, db, make_school, make_staff
):
    """MN-1, the durable invariant: every staff route hangs off StaffDep, so a session belonging to
    a non-live school must have NO side effect — whatever minted it. The reviewer's proof-of-concept
    was exactly this call: it sent real mail from no-reply@youhue.app to an attacker-chosen address.

    The active-school control proves the 403 is the school-status gate, not a malformed request."""
    pending = make_school(code="PEND-1", status=SchoolStatus.pending)
    pending_staff = make_staff(pending, email="head@pending.edu")
    token = _mint_staff_session(db, pending_staff)

    denied = client.post(
        NOTIFY,
        headers=_auth(token),
        json={"recipient_id": str(pending_staff.id), "type": "click here", "payload": {}},
    )
    assert denied.status_code == 403
    assert db.query(Notification).count() == 0  # no message queued -> no mail can ever leave

    live = make_school(code="LIVE-1", status=SchoolStatus.active)
    live_staff = make_staff(live, email="head@live.edu")
    allowed = client.post(
        NOTIFY,
        headers=_auth(_mint_staff_session(db, live_staff)),
        json={"recipient_id": str(live_staff.id), "type": "alert", "payload": {}},
    )
    assert allowed.status_code == 202


def test_deactivated_staff_of_a_live_school_is_also_refused(client, db, make_school, make_staff):
    """Same choke point, other half: the account itself must be active, so a session that outlives
    a deactivation stops working immediately rather than at token expiry."""
    school = make_school(code="LIVE-1", status=SchoolStatus.active)
    staff = make_staff(school, email="ex@live.edu")
    token = _mint_staff_session(db, staff)
    staff.status = StaffStatus.deactivated
    db.commit()

    r = client.get(NOTIFY, headers=_auth(token))
    assert r.status_code == 403


# --- MN-2 / Scenario 2: a pending school is not live (GATE G-1) --------------------------------
def test_pending_school_cannot_run_student_check_in(client, db, make_student):
    """End-to-end over HTTP with a positive control. The previous version of this test asserted a
    pre-existing domain helper returned None, which is true for ANY non-active school and stayed
    green while the whole staff surface was open."""
    school_id = client.post(REGISTER, json=_payload()).json()["school_id"]
    school = db.get(School, school_id)
    student = make_student(school, name="Amy")

    refused = client.post(
        STUDENT_SIGNIN,
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    assert refused.status_code == 400  # not live -> the code does not resolve

    # Positive control: the ONLY thing that changed is approval, and check-in then works — so the
    # 400 above was the pending gate, not a missing student or a bad code.
    school.status = SchoolStatus.active
    db.commit()
    allowed = client.post(
        STUDENT_SIGNIN,
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    assert allowed.status_code == 200
    assert allowed.json()["session_token"]


# --- MN-3 / Scenario 3: a later teacher must not create a duplicate ----------------------------
def test_later_teacher_of_an_existing_school_gets_a_terminal_conflict(client, db, make_school):
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    r = client.post(REGISTER, json=_payload(registrant_email="new.teacher@oakwood.edu"))

    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["code"] == "school_exists"
    # No join endpoint exists anywhere in the backend (FR-02-03 is not built), so the contract must
    # not send the frontend to one; and an anonymous caller is told no tenant UUID.
    assert "action" not in detail
    assert "school_id" not in detail
    assert db.query(School).count() == 1  # no duplicate created


def test_name_match_ignores_case_and_surrounding_and_internal_whitespace(client, db, make_school):
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    r = client.post(REGISTER, json=_payload(school_name="  oakwood    PRIMARY  "))
    assert r.status_code == 409
    assert db.query(School).count() == 1


def test_a_pending_name_is_also_protected_from_duplication(client, db):
    """Inverted from `test_second_teacher_of_pending_name_creates_new_school`, which codified
    duplicate PENDING schools as intended. The Must-not is "must not create a duplicate school",
    and N identical rows in the approval queue is exactly that."""
    client.post(REGISTER, json=_payload())
    r = client.post(REGISTER, json=_payload(registrant_email="other@oakwood.edu"))
    assert r.status_code == 409
    assert db.query(School).count() == 1


def test_a_rejected_school_releases_its_name_for_a_fresh_registration(client, db, make_school):
    """A rejected registration must not be a permanent dead end for the school it named."""
    make_school(code="OAK-REJ", status=SchoolStatus.rejected, name="Oakwood Primary")
    r = client.post(REGISTER, json=_payload())
    assert r.status_code == 201
    assert db.get(School, r.json()["school_id"]).status == SchoolStatus.pending


def test_duplicate_name_is_refused_by_the_database(db, make_school):
    """The guarantee cannot rest on an application read: prove `uq_schools_name_live` exists and
    bites, which is what makes two concurrent registrations safe."""
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    db.add(School(name="oakwood primary", timezone="UTC", sign_in_code="OTHER-1"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_a_lost_race_is_a_conflict_not_a_duplicate(client, db, make_school, monkeypatch):
    """Simulate the interleaving the reviewer described: the lookup sees nothing (another request
    committed just after it ran), so the INSERT is what discovers the clash. The caller must get the
    same terminal 409, never a second school."""
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    monkeypatch.setattr(identity_db, "get_school_by_name", lambda *_a, **_k: None)

    r = client.post(REGISTER, json=_payload())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "school_exists"
    assert db.query(School).count() == 1


# --- non-enumeration: registration answers no question about who has an account ----------------
def test_registration_does_not_reveal_whether_an_email_is_known(client, db, make_school, make_staff):
    """A known staff email at a live school used to return 403 + that school's UUID, while an
    unknown one returned 201 — an anonymous yes/no oracle over any address, two modules away from
    the constant-time, no-disclosure posture the auth module enforces."""
    live = make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Greenfield High")
    known = make_staff(live, email="real.teacher@greenfield.example", password="Password123")

    seen = client.post(REGISTER, json=_payload(
        school_name="Elmwood Academy", registrant_email="real.teacher@greenfield.example"))
    unseen = client.post(REGISTER, json=_payload(
        school_name="Birchwood Academy", registrant_email="ghost@greenfield.example"))

    assert seen.status_code == unseen.status_code == 201
    assert seen.json().keys() == unseen.json().keys()
    assert seen.json()["status"] == unseen.json()["status"] == "pending"
    # ...and the existing account is untouched: a registration grants nothing anywhere else.
    db.refresh(known)
    assert known.school_id == live.id
    assert known.status == StaffStatus.active
    assert security.verify_password("Password123", str(known.password_hash)) is True


def test_conflict_path_still_runs_a_password_hash(client, make_school, monkeypatch):
    """The 403 branch used to skip the bcrypt the 201 branch ran, which is a timing oracle whatever
    the status code says. Every path must spend one hash."""
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    calls: list[str] = []
    real = security.verify_password
    monkeypatch.setattr(
        security, "verify_password",
        lambda plain, hashed: (calls.append(plain), real(plain, hashed))[1],
    )
    assert client.post(REGISTER, json=_payload()).status_code == 409
    assert len(calls) == 1


# --- MN-4: idempotency on retry [BR-05], and what a replay is NOT ------------------------------
def test_retry_of_a_pending_registration_is_idempotent(client, db):
    first = client.post(REGISTER, json=_payload())
    second = client.post(REGISTER, json=_payload())
    assert first.status_code == second.status_code == 201
    assert first.json()["school_id"] == second.json()["school_id"]
    assert db.query(School).count() == 1
    assert db.query(StaffAccount).count() == 1


def test_replay_requires_the_registrant_password_and_changes_nothing(client, db):
    """The replay path used to check no credential and discard the submitted name: a stranger could
    read back someone else's registration, and the real school head got a 201 for a school they did
    not own and could not sign into. Now a wrong password is simply the conflict everyone else sees."""
    original = client.post(REGISTER, json=_payload()).json()["school_id"]
    before = _staff_of(db, original).password_hash

    replay = client.post(REGISTER, json=_payload(
        school_name="  oakwood primary ", password="not-the-real-password"))

    assert replay.status_code == 409
    assert "school_id" not in replay.json()["detail"]  # the original id is not handed back
    db.expire_all()
    assert db.get(School, original).name == "Oakwood Primary"  # name not silently rewritten
    assert _staff_of(db, original).password_hash == before     # password not silently rotated
    assert db.query(StaffAccount).count() == 1


def test_a_wrong_password_replay_is_indistinguishable_from_a_stranger(client):
    """Both are 'this name is taken' — the response must not tell a probe which one it hit."""
    client.post(REGISTER, json=_payload())
    wrong_password = client.post(REGISTER, json=_payload(password="not-the-real-password"))
    stranger = client.post(REGISTER, json=_payload(registrant_email="someone@else.example"))
    assert wrong_password.status_code == stranger.status_code == 409
    assert wrong_password.json() == stranger.json()


def test_conflict_leaves_the_database_unchanged(client, db, make_school):
    make_school(code="OAK-LIVE", status=SchoolStatus.active, name="Oakwood Primary")
    before = (db.query(School).count(), db.query(StaffAccount).count())
    assert client.post(REGISTER, json=_payload()).status_code == 409
    assert (db.query(School).count(), db.query(StaffAccount).count()) == before


def test_an_unexpected_failure_rolls_the_whole_registration_back(client, db, monkeypatch):
    """The catch-all guard exists so no half-registered school survives. Fail AFTER the school row
    has been flushed, so a missing rollback would leave a school with no owner behind."""
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(identity_db, "create_staff", _boom)
    r = client.post(REGISTER, json=_payload())

    assert r.status_code == 500
    assert r.json()["detail"] == "Registration failed"
    assert db.query(School).count() == 0
    assert db.query(StaffAccount).count() == 0


def test_sign_in_code_exhaustion_fails_closed(client, db, make_school, monkeypatch):
    """Every generated code collides -> give up with a 500 rather than loop or reuse a live code."""
    make_school(code="FIXEDCOD", status=SchoolStatus.active, name="Somewhere Else")
    monkeypatch.setattr(security, "new_school_code", lambda *_a, **_k: "FIXEDCOD")

    r = client.post(REGISTER, json=_payload())
    assert r.status_code == 500
    assert db.query(School).count() == 1  # only the pre-existing one; nothing partial was written


# --- MN-4: server-side validation is the gate -------------------------------------------------
def test_empty_school_name_rejected(client):
    assert client.post(REGISTER, json=_payload(school_name="")).status_code == 422


def test_whitespace_only_school_name_rejected(client):
    assert client.post(REGISTER, json=_payload(school_name="   ")).status_code == 422


def test_invalid_email_rejected(client):
    assert client.post(REGISTER, json=_payload(registrant_email="not-an-email")).status_code == 422


def test_short_password_rejected(client):
    assert client.post(REGISTER, json=_payload(password="short")).status_code == 422


def test_unknown_body_fields_are_rejected_not_ignored(client, db):
    """No privilege escalation via the body — as an enforced property, not an incidental one."""
    r = client.post(REGISTER, json=_payload(role="district", status="active"))
    assert r.status_code == 422
    assert db.query(School).count() == 0


def test_validation_errors_use_the_array_detail_shape(client):
    """Pinned deliberately: `detail` is an ARRAY here and an OBJECT for 409 — the frontend must
    type-guard, so a change to either shape has to break a test first."""
    detail = client.post(REGISTER, json=_payload(password="short")).json()["detail"]
    assert isinstance(detail, list)
    assert {"loc", "msg", "type"} <= set(detail[0])


# --- rate limiting: a public write must not be able to throttle anyone's sign-in ---------------
def test_registration_is_rate_limited(client, db):
    limit = settings.rate_limit_register_per_minute
    for i in range(limit):
        assert client.post(REGISTER, json=_payload(
            school_name=f"School {i}", registrant_email=f"head{i}@oakwood.edu")).status_code == 201
    blocked = client.post(REGISTER, json=_payload(
        school_name="One Too Many", registrant_email="last@oakwood.edu"))
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "Too many requests, slow down"
    assert db.query(School).count() == limit  # the refused request wrote nothing


def test_registration_throttling_does_not_lock_out_staff_sign_in(client, make_school, make_staff):
    """Proven regression: one client exhausting registration used to 429 an unrelated, legitimate
    teacher signing in from the same IP, because both shared a single 10/min bucket."""
    make_staff(make_school(code="LIVE-1"), email="t@live.edu", password="Password123")
    for i in range(settings.rate_limit_register_per_minute + 3):
        client.post(REGISTER, json=_payload(
            school_name=f"School {i}", registrant_email=f"head{i}@oakwood.edu"))

    r = client.post(SIGNIN, json={"email": "t@live.edu", "password": "Password123"})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_forwarded_for_is_honoured_only_when_the_proxy_is_trusted(client, monkeypatch):
    """Behind a proxy the socket peer is the proxy, so the whole internet shares one bucket unless
    X-Forwarded-For is read — but reading it unconditionally lets any caller forge a fresh bucket."""
    limit = settings.rate_limit_register_per_minute

    # Trusted proxy: the left-most X-Forwarded-For identifies the client, so each forwarded IP has
    # its OWN budget and one IP exhausting its budget does not spill onto another's.
    monkeypatch.setattr(settings, "trust_proxy_headers", True)
    for i in range(limit):
        assert client.post(REGISTER, json=_payload(
            school_name=f"A{i}", registrant_email=f"a{i}@oakwood.edu"),
            headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 201
    assert client.post(REGISTER, json=_payload(
        school_name="Blocked A", registrant_email="blocked.a@oakwood.edu"),
        headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 429
    # a genuinely different forwarded client is not collateral damage — separate bucket
    assert client.post(REGISTER, json=_payload(
        school_name="Other Client", registrant_email="other@oakwood.edu"),
        headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 201

    # Untrusted (the default): X-Forwarded-For is IGNORED, so every request keys on the socket peer
    # and shares ONE budget no matter what IP it forges. The socket-peer bucket is still empty here
    # (the phase above keyed on forwarded IPs), so fill it — each with a DIFFERENT forged header —
    # then prove a fresh forged header cannot mint a new bucket to bypass the limit.
    monkeypatch.setattr(settings, "trust_proxy_headers", False)
    for i in range(limit):
        assert client.post(REGISTER, json=_payload(
            school_name=f"B{i}", registrant_email=f"b{i}@oakwood.edu"),
            headers={"X-Forwarded-For": f"9.9.9.{i}"}).status_code == 201
    assert client.post(REGISTER, json=_payload(
        school_name="Blocked B", registrant_email="blocked.b@oakwood.edu"),
        headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429


# --- OpenAPI: the frontend generates its client from this ---------------------------------------
def test_openapi_documents_every_status_the_endpoint_returns(client):
    responses = client.get("/openapi.json").json()["paths"]["/api/v1/schools"]["post"]["responses"]
    assert {"201", "409", "422", "429", "500"} <= set(responses)
    conflict = responses["409"]["content"]["application/json"]["schema"]
    assert conflict["$ref"].endswith("ConflictResponse")


def test_response_status_is_pinned_to_pending_in_the_schema(client):
    schema = client.get("/openapi.json").json()["components"]["schemas"]["RegisterSchoolResponse"]
    assert schema["properties"]["status"]["const"] == "pending"


def test_school_id_is_a_uuid(client):
    school_id = client.post(REGISTER, json=_payload()).json()["school_id"]
    assert uuid.UUID(school_id)
