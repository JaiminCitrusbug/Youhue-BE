"""INFRA-02 isolation: cross-tenant reads denied (403) as the disallowed actor, over HTTP."""
import uuid

import pytest
from sqlalchemy.exc import DatabaseError

from src.application.isolation import services as isolation
from src.domain.compliance.models import AuditLog
from src.domain.identity.models import Student

SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str, pw: str = "Password123") -> str:
    return client.post(SIGNIN, json={"email": email, "password": pw}).json()["access_token"]


def test_staff_reads_own_class_student(client, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class):
    school = make_school(code="A")
    staff = make_staff(school, email="a@oakwood.edu")
    student = make_student(school, name="Amy")
    klass = make_class(school)
    grant_class_access(staff, klass)
    add_to_class(klass, student)
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


def test_read_writes_immutable_audit(client, db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class):
    school = make_school(code="A")
    staff = make_staff(school, email="a@oakwood.edu")
    student = make_student(school)
    klass = make_class(school)
    grant_class_access(staff, klass)
    add_to_class(klass, student)
    client.get(f"/api/v1/students/{student.id}", headers=_auth(_staff_token(client, "a@oakwood.edu")))
    rows = db.query(AuditLog).all()
    assert any(r.action == "student.read" and r.target == str(student.id) for r in rows)


def test_get_scoped_rejects_other_school(db, make_school, make_student):
    school_a = make_school(code="A")
    school_b = make_school(code="B")
    student_b = make_student(school_b)
    assert isolation.get_scoped(db, Student, student_b.id, school_a.id) is None
    assert isolation.get_scoped(db, Student, student_b.id, school_b.id) is not None


def test_denied_cross_tenant_read_is_audited(client, db, make_school, make_staff, make_student):
    make_staff(make_school(code="A"), email="a@oakwood.edu")
    student_b = make_student(make_school(code="B"))
    client.get(f"/api/v1/students/{student_b.id}", headers=_auth(_staff_token(client, "a@oakwood.edu")))
    assert any(r.action == "student.read.denied" for r in db.query(AuditLog).all())  # intrusion signal


def test_audit_log_is_immutable(db):
    isolation.audit(db, actor_id=uuid.uuid4(), action="x", target="y")
    db.commit()
    with pytest.raises(DatabaseError):  # DB trigger blocks UPDATE on the append-only table
        db.query(AuditLog).update({AuditLog.action: "hacked"})
        db.flush()
    db.rollback()


def test_get_scoped_via_student_isolates(db, make_school, make_staff, make_student):
    from src.domain.risk.models import SupportiveNote
    school_a = make_school(code="A")
    school_b = make_school(code="B")
    student_b = make_student(school_b)
    sender = make_staff(school_b, email="s@oakwood.edu")
    note = SupportiveNote(student_id=student_b.id, sender_id=sender.id, body="x", is_private=True)
    db.add(note)
    db.commit()
    # a student-child row (no own school_id) isolates via its student's school
    assert isolation.get_scoped_via_student(db, SupportiveNote, note.id, school_a.id) is None
    assert isolation.get_scoped_via_student(db, SupportiveNote, note.id, school_b.id) is not None
