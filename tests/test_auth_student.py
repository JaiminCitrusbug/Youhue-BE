"""Student passwordless sign-in — FR-01-02 (@FR-01-02 scenarios) + INFRA-01 single-active-device.

Covers: code (school + class) and qr_token sign-in, name/avatar selection, mutual-exclusivity,
single-use qr replay, invalid/expired code (400), student-not-in-class (404), rate-limit (429),
single-active-device, student-session scope isolation, and the class-access issuance service.
"""
import uuid

from src.application.classes import services as classes_svc

SIGNIN = "/api/v1/auth/student/sign-in"
ROSTER = "/api/v1/auth/student/roster"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _issue(db, klass):
    access = classes_svc.issue_access(db, klass.id)
    db.commit()
    return access


# ---- positive: code + qr_token + name/avatar selection --------------------------------------

def test_signin_with_school_code_success(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    r = client.post(SIGNIN, json={"school_or_class_code": "OAK-77", "student_id": str(student.id)})
    assert r.status_code == 200
    body = r.json()
    assert body["session_token"]
    assert body["student_id"] == str(student.id)
    assert body["age_band"] == "b8_11"


def test_signin_with_class_join_code_success(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    student = make_student(school)
    klass = make_class(school)
    add_to_class(klass, student)
    access = _issue(db, klass)
    r = client.post(
        SIGNIN, json={"school_or_class_code": access.join_code, "student_id": str(student.id)}
    )
    assert r.status_code == 200
    assert r.json()["session_token"]


def test_signin_by_scanning_qr_token_success(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    student = make_student(school)
    klass = make_class(school)
    add_to_class(klass, student)
    access = _issue(db, klass)
    r = client.post(SIGNIN, json={"qr_token": access.qr_token, "student_id": str(student.id)})
    assert r.status_code == 200
    assert r.json()["session_token"]


# ---- negative: invalid / expired code (400) --------------------------------------------------

def test_invalid_code_refused_400(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    r = client.post(SIGNIN, json={"school_or_class_code": "WRONG", "student_id": str(student.id)})
    assert r.status_code == 400


def test_invalid_qr_token_refused_400(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    r = client.post(SIGNIN, json={"qr_token": "not-a-real-token", "student_id": str(student.id)})
    assert r.status_code == 400


def test_inactive_school_code_refused_400(client, make_school, make_student):
    from src.constants.enums import SchoolStatus

    school = make_school(code="PENDING-1", status=SchoolStatus.pending)
    student = make_student(school)
    r = client.post(
        SIGNIN, json={"school_or_class_code": "PENDING-1", "student_id": str(student.id)}
    )
    assert r.status_code == 400


# ---- negative: student not in class / school (404) -------------------------------------------

def test_student_not_in_class_404(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    member = make_student(school, name="Amy")
    outsider = make_student(school, name="Ben")  # same school, NOT added to the class
    klass = make_class(school)
    add_to_class(klass, member)
    access = _issue(db, klass)
    r = client.post(
        SIGNIN, json={"school_or_class_code": access.join_code, "student_id": str(outsider.id)}
    )
    assert r.status_code == 404


def test_student_from_another_school_404(client, make_school, make_student):
    make_school(code="AAA-1", name="A")  # valid code, but the student below is not in this school
    school_b = make_school(code="BBB-1", name="B")
    student_b = make_student(school_b)
    r = client.post(
        SIGNIN, json={"school_or_class_code": "AAA-1", "student_id": str(student_b.id)}
    )
    assert r.status_code == 404


def test_unknown_student_id_404(client, make_school):
    make_school(code="OAK-77")
    r = client.post(
        SIGNIN, json={"school_or_class_code": "OAK-77", "student_id": str(uuid.uuid4())}
    )
    assert r.status_code == 404


# ---- edge/security: mutual exclusivity + single-use qr ---------------------------------------

def test_code_and_qr_together_refused_400(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    student = make_student(school)
    klass = make_class(school)
    add_to_class(klass, student)
    access = _issue(db, klass)
    r = client.post(
        SIGNIN,
        json={
            "school_or_class_code": access.join_code,
            "qr_token": access.qr_token,
            "student_id": str(student.id),
        },
    )
    assert r.status_code == 400


def test_neither_code_nor_qr_refused_400(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    r = client.post(SIGNIN, json={"student_id": str(student.id)})
    assert r.status_code == 400


def test_qr_token_single_use_per_session_replay_refused(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    student = make_student(school)
    klass = make_class(school)
    add_to_class(klass, student)
    access = _issue(db, klass)
    first = client.post(SIGNIN, json={"qr_token": access.qr_token, "student_id": str(student.id)})
    assert first.status_code == 200
    # same student re-presents the same qr_token while the session is active -> single-use replay
    replay = client.post(SIGNIN, json={"qr_token": access.qr_token, "student_id": str(student.id)})
    assert replay.status_code == 400


def test_persistent_qr_serves_the_whole_class(
    client, db, make_school, make_student, make_class, add_to_class
):
    # the QR is persistent per class: a different student consumes the same token fine
    school = make_school(code="OAK-77")
    amy = make_student(school, name="Amy")
    ben = make_student(school, name="Ben")
    klass = make_class(school)
    add_to_class(klass, amy)
    add_to_class(klass, ben)
    access = _issue(db, klass)
    assert client.post(SIGNIN, json={"qr_token": access.qr_token, "student_id": str(amy.id)}).status_code == 200
    assert client.post(SIGNIN, json={"qr_token": access.qr_token, "student_id": str(ben.id)}).status_code == 200


# ---- roster picker (SC-021 real name/avatar list, not a fixture) -----------------------------

def test_roster_by_school_code_returns_real_students(client, make_school, make_student):
    school = make_school(code="OAK-77")
    amy = make_student(school, name="Amy")
    ben = make_student(school, name="Ben")
    r = client.get(ROSTER, params={"school_or_class_code": "OAK-77"})
    assert r.status_code == 200
    names = {s["display_name"] for s in r.json()["students"]}
    ids = {s["id"] for s in r.json()["students"]}
    assert names == {"Amy", "Ben"}
    assert ids == {str(amy.id), str(ben.id)}


def test_roster_by_class_join_code_scopes_to_the_class(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    member = make_student(school, name="Amy")
    make_student(school, name="Ben")  # same school, NOT in this class — must not appear
    klass = make_class(school)
    add_to_class(klass, member)
    access = _issue(db, klass)
    r = client.get(ROSTER, params={"school_or_class_code": access.join_code})
    assert r.status_code == 200
    names = {s["display_name"] for s in r.json()["students"]}
    assert names == {"Amy"}  # the outsider is NOT in this class's roster


def test_roster_excludes_inactive_students(client, db, make_school, make_student):
    from src.constants.enums import StudentStatus

    school = make_school(code="OAK-77")
    make_student(school, name="Amy")
    left = make_student(school, name="Ben")
    left.status = StudentStatus.inactive
    db.commit()
    r = client.get(ROSTER, params={"school_or_class_code": "OAK-77"})
    assert r.status_code == 200
    names = {s["display_name"] for s in r.json()["students"]}
    assert names == {"Amy"}


def test_roster_invalid_code_refused_400(client):
    r = client.get(ROSTER, params={"school_or_class_code": "NOPE"})
    assert r.status_code == 400


def test_roster_by_qr_token_scopes_to_the_class(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    member = make_student(school, name="Amy")
    make_student(school, name="Ben")  # same school, NOT in this class
    klass = make_class(school)
    add_to_class(klass, member)
    access = _issue(db, klass)
    r = client.get(ROSTER, params={"qr_token": access.qr_token})
    assert r.status_code == 200
    names = {s["display_name"] for s in r.json()["students"]}
    assert names == {"Amy"}


def test_roster_neither_code_nor_qr_refused_400(client):
    r = client.get(ROSTER)
    assert r.status_code == 400


def test_roster_code_and_qr_together_refused_400(client, db, make_school, make_class):
    school = make_school(code="OAK-77")
    klass = make_class(school)
    access = _issue(db, klass)
    r = client.get(
        ROSTER, params={"school_or_class_code": access.join_code, "qr_token": access.qr_token}
    )
    assert r.status_code == 400


def test_roster_matches_what_sign_in_will_accept(client, make_school, make_student):
    # The picker and sign-in resolve the SAME code the SAME way — a student returned by the
    # roster is guaranteed to be a valid sign-in target for that same code.
    school = make_school(code="OAK-77")
    student = make_student(school, name="Amy")
    roster = client.get(ROSTER, params={"school_or_class_code": "OAK-77"}).json()["students"]
    picked_id = roster[0]["id"]
    assert picked_id == str(student.id)
    r = client.post(SIGNIN, json={"school_or_class_code": "OAK-77", "student_id": picked_id})
    assert r.status_code == 200


# ---- single-active-device (INFRA-01 / FR-01-08) ----------------------------------------------

def test_single_active_device(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    t1 = client.post(
        SIGNIN,
        json={"school_or_class_code": "OAK-77", "student_id": str(student.id), "device_id": "dev1"},
    ).json()["session_token"]
    assert client.get("/api/v1/me", headers=_auth(t1)).status_code == 200
    t2 = client.post(
        SIGNIN,
        json={"school_or_class_code": "OAK-77", "student_id": str(student.id), "device_id": "dev2"},
    ).json()["session_token"]
    assert client.get("/api/v1/me", headers=_auth(t1)).status_code == 401
    assert client.get("/api/v1/me", headers=_auth(t2)).status_code == 200


# ---- rate-limit (429) ------------------------------------------------------------------------

def test_signin_is_rate_limited_429(client, make_school, make_student):
    from config.env_config import settings

    school = make_school(code="OAK-77")
    student = make_student(school)
    payload = {"school_or_class_code": "OAK-77", "student_id": str(student.id)}
    last = None
    for _ in range(settings.rate_limit_signin_per_minute + 1):
        last = client.post(SIGNIN, json=payload)
    assert last is not None
    assert last.status_code == 429


# ---- failed-attempt escalation / abuse resistance (M1 hardening) -----------------------------
# Exercised at the service layer so the (IP + code) key can be varied and the per-minute HTTP
# limiter (a router Depends) doesn't mask the longer-window escalation being asserted here.

def test_repeated_failed_attempts_on_a_code_escalate_to_429(db, make_school):
    import pytest
    from fastapi import HTTPException

    from config.env_config import settings
    from src.application.auth import student as student_svc

    make_school(code="OAK-77")
    ip = "10.0.0.1"
    # A valid code + bogus student_id fails 404 each time (a code-enumeration / probing pattern).
    for _ in range(settings.student_signin_max_attempts):
        with pytest.raises(HTTPException) as exc:
            student_svc.sign_in(
                db, school_or_class_code="OAK-77", qr_token=None,
                student_id=uuid.uuid4(), client_ip=ip,
            )
        assert exc.value.status_code == 404
    # The next attempt from the SAME origin against THAT code is now refused (429), not 404.
    with pytest.raises(HTTPException) as exc:
        student_svc.sign_in(
            db, school_or_class_code="OAK-77", qr_token=None,
            student_id=uuid.uuid4(), client_ip=ip,
        )
    assert exc.value.status_code == 429


def test_bad_actor_cannot_lock_the_whole_class_out(db, make_school, make_student):
    import pytest
    from fastapi import HTTPException

    from config.env_config import settings
    from src.application.auth import student as student_svc

    school = make_school(code="OAK-77")
    student = make_student(school)
    attacker_ip = "10.0.0.1"
    # Griefer hammers the class's SHARED code from one origin until refused.
    for _ in range(settings.student_signin_max_attempts + 3):
        try:
            student_svc.sign_in(
                db, school_or_class_code="OAK-77", qr_token=None,
                student_id=uuid.uuid4(), client_ip=attacker_ip,
            )
        except HTTPException:
            pass
    with pytest.raises(HTTPException) as exc:
        student_svc.sign_in(
            db, school_or_class_code="OAK-77", qr_token=None,
            student_id=uuid.uuid4(), client_ip=attacker_ip,
        )
    assert exc.value.status_code == 429  # attacker's own origin is throttled
    # ...but a legitimate student on a DIFFERENT device/IP signs in fine with the SAME shared code:
    # no class-wide lockout, no DoS lever.
    ok = student_svc.sign_in(
        db, school_or_class_code="OAK-77", qr_token=None,
        student_id=student.id, client_ip="192.168.1.50",
    )
    assert ok.session_token


def test_legit_student_success_breaks_the_failure_streak_same_ip(db, make_school, make_student):
    import pytest
    from fastapi import HTTPException

    from config.env_config import settings
    from src.application.auth import student as student_svc

    school = make_school(code="OAK-77")
    student = make_student(school)
    ip = "10.0.0.9"
    # Just BELOW the threshold: honest failures (wrong student_id -> 404) on this (ip, code).
    for _ in range(settings.student_signin_max_attempts - 1):
        with pytest.raises(HTTPException) as exc:
            student_svc.sign_in(
                db, school_or_class_code="OAK-77", qr_token=None,
                student_id=uuid.uuid4(), client_ip=ip,
            )
        assert exc.value.status_code == 404
    # The real student then signs in (200); the recorded success resets the consecutive-fail count.
    ok = student_svc.sign_in(
        db, school_or_class_code="OAK-77", qr_token=None,
        student_id=student.id, client_ip=ip,
    )
    assert ok.session_token
    # Because the streak reset, a fresh attempt is a normal 404 — NOT a pre-emptive 429 lockout.
    with pytest.raises(HTTPException) as exc:
        student_svc.sign_in(
            db, school_or_class_code="OAK-77", qr_token=None,
            student_id=uuid.uuid4(), client_ip=ip,
        )
    assert exc.value.status_code == 404


# ---- student-session scope isolation ---------------------------------------------------------

def test_student_session_scope_is_student_only(client, make_school, make_student):
    school = make_school(code="OAK-77")
    student = make_student(school)
    token = client.post(
        SIGNIN, json={"school_or_class_code": "OAK-77", "student_id": str(student.id)}
    ).json()["session_token"]
    me = client.get("/api/v1/me", headers=_auth(token))
    assert me.status_code == 200
    assert me.json()["kind"] == "student"
    assert me.json()["subject_id"] == str(student.id)
    # the student surface exposes NO staff feature: a staff-guarded route rejects the student
    staff_route = client.get(f"/api/v1/students/{student.id}", headers=_auth(token))
    assert staff_route.status_code == 403


# ---- class-access issuance service (FR-01-02 provides it; teacher UI is FR-01-09) ------------

def test_issue_access_generates_credentials(db, make_school, make_class):
    school = make_school(code="OAK-77")
    klass = make_class(school)
    access = classes_svc.issue_access(db, klass.id)
    db.commit()
    assert access.join_code and access.qr_token
    assert access.class_id == klass.id


def test_issue_access_is_persistent(db, make_school, make_class):
    school = make_school(code="OAK-77")
    klass = make_class(school)
    first = classes_svc.issue_access(db, klass.id)
    db.commit()
    again = classes_svc.issue_access(db, klass.id)
    db.commit()
    assert first.join_code == again.join_code
    assert first.qr_token == again.qr_token


def test_rotate_access_changes_credentials(db, make_school, make_class):
    school = make_school(code="OAK-77")
    klass = make_class(school)
    first = classes_svc.issue_access(db, klass.id)
    db.commit()
    rotated = classes_svc.rotate_access(db, klass.id)
    db.commit()
    assert rotated.join_code != first.join_code
    assert rotated.qr_token != first.qr_token


def test_issue_access_unknown_class_404(db):
    import pytest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        classes_svc.issue_access(db, uuid.uuid4())
    assert exc.value.status_code == 404


def test_rotated_old_qr_no_longer_signs_in(
    client, db, make_school, make_student, make_class, add_to_class
):
    school = make_school(code="OAK-77")
    student = make_student(school)
    klass = make_class(school)
    add_to_class(klass, student)
    first = classes_svc.issue_access(db, klass.id)
    db.commit()
    classes_svc.rotate_access(db, klass.id)
    db.commit()
    # the old qr_token was replaced by rotation -> no longer resolves (400 invalid/expired)
    r = client.post(SIGNIN, json={"qr_token": first.qr_token, "student_id": str(student.id)})
    assert r.status_code == 400
