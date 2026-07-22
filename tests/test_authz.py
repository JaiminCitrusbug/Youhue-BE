"""INFRA-03 roles & permissions: role/class scoping + forced MFA for privileged roles."""
import pytest
from fastapi import HTTPException

from src.application import authz
from src.domain.enums import StaffRole

SIGNIN = "/api/v1/auth/staff/sign-in"


def test_require_roles_allows_and_denies(make_school, make_staff):
    teacher = make_staff(make_school(), email="t@oakwood.edu", role=StaffRole.teacher)
    authz.require_roles(teacher, StaffRole.teacher, StaffRole.leadership)  # no raise
    with pytest.raises(HTTPException) as exc:
        authz.require_roles(teacher, StaffRole.leadership)
    assert exc.value.status_code == 403


def test_teacher_class_scope(db, make_school, make_staff, make_student, make_class, grant_class_access, add_to_class):
    school = make_school()
    teacher = make_staff(school, email="t@oakwood.edu", role=StaffRole.teacher)
    own = make_student(school, name="Own")
    other = make_student(school, name="Other")
    klass = make_class(school)
    grant_class_access(teacher, klass)
    add_to_class(klass, own)
    assert authz.can_access_student(db, teacher, own) is True
    assert authz.can_access_student(db, teacher, other) is False  # same school, not their class


def test_teacher_without_class_sees_no_student(db, make_school, make_staff, make_student):
    school = make_school()
    teacher = make_staff(school, email="t@oakwood.edu", role=StaffRole.teacher)
    assert authz.can_access_student(db, teacher, make_student(school)) is False


def test_leadership_sees_whole_school(db, make_school, make_staff, make_student):
    school = make_school()
    lead = make_staff(school, email="l@oakwood.edu", role=StaffRole.leadership)
    assert authz.can_access_student(db, lead, make_student(school)) is True  # no class needed


def test_cross_school_never(db, make_school, make_staff, make_student):
    lead_a = make_staff(make_school(code="A"), email="l@oakwood.edu", role=StaffRole.leadership)
    student_b = make_student(make_school(code="B"))
    assert authz.can_access_student(db, lead_a, student_b) is False


def test_leadership_signin_forces_mfa(client, make_school, make_staff, monkeypatch):
    import src.application.auth.staff as staff_mod
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: None)
    # mfa_enabled=False, but the leadership role forces MFA
    make_staff(make_school(), email="l@oakwood.edu", password="Password123", role=StaffRole.leadership, mfa=False)
    r = client.post(SIGNIN, json={"email": "l@oakwood.edu", "password": "Password123"})
    assert r.status_code == 200
    assert r.json()["mfa_required"] is True


def test_teacher_signin_no_mfa(client, make_school, make_staff):
    make_staff(make_school(), email="t@oakwood.edu", password="Password123", role=StaffRole.teacher)
    assert client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "Password123"}).json()["mfa_required"] is False


def test_role_requires_mfa_helper():
    assert authz.role_requires_mfa(StaffRole.district) is True
    assert authz.role_requires_mfa(StaffRole.teacher) is False


def test_district_denied_student_level(db, make_school, make_staff, make_student):
    # SRS §8: district sees aggregates only — never a student-level row (review B1 fix)
    school = make_school()
    district = make_staff(school, email="d@oakwood.edu", role=StaffRole.district)
    assert authz.can_access_student(db, district, make_student(school)) is False
