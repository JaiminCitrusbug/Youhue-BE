"""FR-13-05 (SC-041) — POST /api/v1/students/{id}/notes: a teacher's quiet, private, supportive
note to a specific student. NEG gate (the whole point of this ticket): visible to no one other
than the intended student and the sender — never public, never visible to peers or unrelated
staff, not even a co-teacher who otherwise shares class access to the same student.

  S1  A teacher sends a note; it is delivered privately to that student.
      -> test_send_note_201_and_delivered_to_student
  S2  NEG — the note is not visible to anyone but the intended student and the sender.
      -> test_note_not_visible_to_other_student
      -> test_note_not_visible_to_unrelated_staff
      -> test_note_not_visible_to_co_teacher_with_shared_class_access
  MN  Teacher may only note their OWN student (403 otherwise); school-scoped.
      -> test_send_note_403_not_your_student
      -> test_send_note_403_cross_school
      -> test_send_note_404_unknown_student
  MN  ACID + idempotency-where-retried [Baseline BR-05].
      -> test_send_note_idempotent_retry_returns_same_note_no_duplicate_row
      -> test_send_note_different_body_is_a_genuinely_new_note
  MN  A 500 is surfaced, never silently dropped.
      -> test_send_note_500_never_dropped
  MN  Structured logs: fr_13_05_success / _forbidden.
      -> test_send_note_success_logs_fr_13_05_success
      -> test_send_note_forbidden_logs_fr_13_05_forbidden
  MN  GET /flags/{id}/student — the guided-response "send a private note" navigation's own
      resolution read (FR-13-05's addition, same involved-teacher gate as FR-13-04's /guidance).
      -> test_flag_student_resolves_for_involved_teacher
      -> test_flag_student_403_for_uninvolved_teacher
      -> test_flag_student_404_unknown_flag
"""
from src.application.students import services as students_svc
from src.constants.enums import StaffClassScope, StaffRole
from src.domain.risk import services as risk_db
from src.domain.risk.models import Flag, SupportiveNote

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"
STUDENT_SIGNIN = "/api/v1/auth/student/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str = "t@oakwood.edu") -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _student_token(client, school, student) -> str:
    r = client.post(
        STUDENT_SIGNIN, json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)}
    )
    return r.json()["session_token"]


def _involved_setup(db, make_school, make_staff, make_student, make_class, grant_class_access,
                     add_to_class, *, email: str = "t@oakwood.edu"):
    school = make_school()
    teacher = make_staff(school, email=email)
    student = make_student(school)
    klass = make_class(school)
    grant_class_access(teacher, klass, scope=StaffClassScope.owner)
    add_to_class(klass, student)
    return school, teacher, student, klass


def _notes_url(student_id) -> str:
    return f"/api/v1/students/{student_id}/notes"


# ---- Scenario 1: send a private note, delivered privately -------------------------------------

def test_send_note_201_and_delivered_to_student(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    r = client.post(
        _notes_url(student.id), json={"body": "Hi — I noticed today was tough. I'm here."},
        headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 201
    note_id = r.json()["note_id"]
    assert note_id

    student_token = _student_token(client, school, student)
    r2 = client.get("/api/v1/students/me/notes", headers=_auth(student_token))
    assert r2.status_code == 200
    notes = r2.json()["notes"]
    assert len(notes) == 1
    assert notes[0]["note_id"] == note_id
    assert notes[0]["body"] == "Hi — I noticed today was tough. I'm here."


# ---- Scenario 2 (NEG — the gate): visible to no one but the student and the sender ------------

def test_note_not_visible_to_other_student(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, teacher, student, klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    other_student = make_student(school, name="Ben")
    add_to_class(klass, other_student)
    client.post(
        _notes_url(student.id), json={"body": "private note"}, headers=_auth(_staff_token(client)),
    )

    other_token = _student_token(client, school, other_student)
    r = client.get("/api/v1/students/me/notes", headers=_auth(other_token))
    assert r.status_code == 200
    assert r.json()["notes"] == []


def test_note_not_visible_to_unrelated_staff(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    client.post(
        _notes_url(student.id), json={"body": "private note"}, headers=_auth(_staff_token(client)),
    )
    make_staff(school, email="unrelated@oakwood.edu")  # no class access to the student at all
    r = client.get(
        _notes_url(student.id), headers=_auth(_staff_token(client, "unrelated@oakwood.edu")),
    )
    assert r.status_code == 403  # not even student-access, let alone note visibility


def test_note_not_visible_to_co_teacher_with_shared_class_access(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    """The crucial NEG nuance: a co-teacher who legitimately shares CLASS access to the same
    student (so `authz.require_student_access` would let them through) still sees NO notes they
    didn't send themselves — sharing student access never implies sharing note visibility."""
    school, teacher, student, klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    co_teacher = make_staff(school, email="co@oakwood.edu", role=StaffRole.teacher)
    grant_class_access(co_teacher, klass, scope=StaffClassScope.shared)
    client.post(
        _notes_url(student.id), json={"body": "private note"}, headers=_auth(_staff_token(client)),
    )

    r = client.get(_notes_url(student.id), headers=_auth(_staff_token(client, "co@oakwood.edu")))
    assert r.status_code == 200  # co-teacher DOES have student access...
    assert r.json()["notes"] == []  # ...but sees none of the other teacher's notes

    # the sender, in contrast, sees their own note via the same read path
    r2 = client.get(_notes_url(student.id), headers=_auth(_staff_token(client)))
    assert r2.status_code == 200
    assert len(r2.json()["notes"]) == 1


# ---- Teacher may only note their OWN student (403); school-scoped -----------------------------

def test_send_note_403_not_your_student(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    make_staff(school, email="other@oakwood.edu")  # no class access to the student
    r = client.post(
        _notes_url(student.id), json={"body": "hello"},
        headers=_auth(_staff_token(client, "other@oakwood.edu")),
    )
    assert r.status_code == 403


def test_send_note_403_cross_school(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    _school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    school_b = make_school(code="BBB-5", name="B5")
    make_staff(school_b, email="b5@oakwood.edu")
    r = client.post(
        _notes_url(student.id), json={"body": "hello"}, headers=_auth(_staff_token(client, "b5@oakwood.edu")),
    )
    assert r.status_code == 403


def test_send_note_404_unknown_student(client, make_school, make_staff):
    import uuid

    school = make_school()
    make_staff(school)
    r = client.post(
        _notes_url(uuid.uuid4()), json={"body": "hello"}, headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 404


# ---- ACID + idempotency-where-retried [Baseline BR-05] -----------------------------------------

def test_send_note_idempotent_retry_returns_same_note_no_duplicate_row(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    token = _staff_token(client)
    body = {"body": "Hi Liam, I'm here for you."}
    r1 = client.post(_notes_url(student.id), json=body, headers=_auth(token))
    r2 = client.post(_notes_url(student.id), json=body, headers=_auth(token))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["note_id"] == r2.json()["note_id"]
    assert db.query(SupportiveNote).filter(SupportiveNote.student_id == student.id).count() == 1


def test_send_note_different_body_is_a_genuinely_new_note(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    token = _staff_token(client)
    r1 = client.post(_notes_url(student.id), json={"body": "first note"}, headers=_auth(token))
    r2 = client.post(_notes_url(student.id), json={"body": "second, different note"}, headers=_auth(token))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["note_id"] != r2.json()["note_id"]
    assert db.query(SupportiveNote).filter(SupportiveNote.student_id == student.id).count() == 2


# ---- a 500 is surfaced, never silently dropped --------------------------------------------------

def test_send_note_500_never_dropped(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    monkeypatch,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(students_svc.risk_db, "create_supportive_note", _boom)
    calls: list[str] = []
    monkeypatch.setattr(
        students_svc.logger, "exception", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = client.post(
        _notes_url(student.id), json={"body": "hello"}, headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 500


# ---- structured logs: fr_13_05_success / _forbidden --------------------------------------------

def test_send_note_success_logs_fr_13_05_success(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    monkeypatch,
):
    calls: list[str] = []
    monkeypatch.setattr(
        students_svc.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    r = client.post(
        _notes_url(student.id), json={"body": "hello"}, headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 201
    assert any(c.startswith("fr_13_05_success") and "action=send_note" in c for c in calls)


def test_send_note_forbidden_logs_fr_13_05_forbidden(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    monkeypatch,
):
    calls: list[str] = []
    monkeypatch.setattr(
        students_svc.logger, "warning", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    make_staff(school, email="other3@oakwood.edu")
    r = client.post(
        _notes_url(student.id), json={"body": "hello"},
        headers=_auth(_staff_token(client, "other3@oakwood.edu")),
    )
    assert r.status_code == 403
    assert any(
        c.startswith("fr_13_05_forbidden") and "reason=out_of_scope" in c for c in calls
    )


# ---- GET /flags/{id}/student — guided-response navigation's own resolution read ----------------

def _mk_flag(db, student, school) -> Flag:
    from src.constants.enums import FlagType

    flag = risk_db.create_flag(
        db, student_id=student.id, school_id=school.id, checkin_id=None,
        flag_type=FlagType.concern_word, risk_score=0.90, band=None,
    )
    db.commit()
    db.refresh(flag)
    return flag


def test_flag_student_resolves_for_involved_teacher(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    flag = _mk_flag(db, student, school)
    r = client.get(f"/api/v1/flags/{flag.id}/student", headers=_auth(_staff_token(client)))
    assert r.status_code == 200
    assert r.json() == {"student_id": str(student.id), "student_name": student.display_name}


def test_flag_student_403_for_uninvolved_teacher(
    client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
):
    school, _teacher, student, _klass = _involved_setup(
        db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class,
    )
    flag = _mk_flag(db, student, school)
    make_staff(school, email="other4@oakwood.edu")
    r = client.get(
        f"/api/v1/flags/{flag.id}/student", headers=_auth(_staff_token(client, "other4@oakwood.edu")),
    )
    assert r.status_code == 403


def test_flag_student_404_unknown_flag(client, make_school, make_staff):
    import uuid

    school = make_school()
    make_staff(school)
    r = client.get(f"/api/v1/flags/{uuid.uuid4()}/student", headers=_auth(_staff_token(client)))
    assert r.status_code == 404
