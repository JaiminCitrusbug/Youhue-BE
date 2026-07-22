"""INFRA-02 isolation: cross-tenant reads denied (403) as the disallowed actor, over HTTP."""
from src.application import isolation
from src.infrastructure.models.compliance import AuditLog
from src.infrastructure.models.identity import Student

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str, pw: str = "Password123") -> str:
    return client.post(SIGNIN, json={"email": email, "password": pw}).json()["access_token"]


def test_staff_reads_own_school_student(client, make_school, make_staff, make_student):
    school = make_school(code="A")
    make_staff(school, email="a@oakwood.edu")
    student = make_student(school, name="Amy")
    r = client.get(f"/api/v1/students/{student.id}", headers=_auth(_staff_token(client, "a@oakwood.edu")))
    assert r.status_code == 200
    assert r.json()["display_name"] == "Amy"


def test_cross_tenant_read_denied(client, make_school, make_staff, make_student):
    school_a = make_school(code="A")
    make_staff(school_a, email="a@oakwood.edu")
    school_b = make_school(code="B")
    student_b = make_student(school_b, name="Ben")
    # School A staff attempts School B's student -> 403, no data returned
    r = client.get(f"/api/v1/students/{student_b.id}", headers=_auth(_staff_token(client, "a@oakwood.edu")))
    assert r.status_code == 403


def test_student_session_cannot_read_student_records(client, make_school, make_student):
    school = make_school(code="A")
    student = make_student(school)
    token = client.post(
        "/api/v1/auth/student/sign-in", json={"school_code": "A", "student_id": str(student.id)}
    ).json()["access_token"]
    assert client.get(f"/api/v1/students/{student.id}", headers=_auth(token)).status_code == 403


def test_read_writes_immutable_audit(client, db, make_school, make_staff, make_student):
    school = make_school(code="A")
    make_staff(school, email="a@oakwood.edu")
    student = make_student(school)
    client.get(f"/api/v1/students/{student.id}", headers=_auth(_staff_token(client, "a@oakwood.edu")))
    rows = db.query(AuditLog).all()
    assert any(r.action == "student.read" and r.target == str(student.id) for r in rows)


def test_get_scoped_rejects_other_school(db, make_school, make_student):
    school_a = make_school(code="A")
    school_b = make_school(code="B")
    student_b = make_student(school_b)
    assert isolation.get_scoped(db, Student, student_b.id, school_a.id) is None
    assert isolation.get_scoped(db, Student, student_b.id, school_b.id) is not None
