"""Cross-tenant guard (FR-01-07), SSO linked-identity, and student-cannot-be-staff (INFRA-01)."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from src.application.auth import sso as sso_svc
from src.constants.enums import SessionKind
from src.domain.auth.models import AuthSession
from src.infrastructure.middlewares.auth_middleware import get_current_staff, require_same_school


class _FakeSession:
    def __init__(self, school_id: uuid.UUID | None) -> None:
        self.school_id = school_id


def test_require_same_school_denies_cross_tenant():
    with pytest.raises(HTTPException) as exc:
        require_same_school(_FakeSession(uuid.uuid4()), uuid.uuid4())
    assert exc.value.status_code == 403


def test_require_same_school_allows_same():
    sid = uuid.uuid4()
    require_same_school(_FakeSession(sid), sid)  # no raise


def test_require_same_school_denies_null_school():
    with pytest.raises(HTTPException) as exc:
        require_same_school(_FakeSession(None), uuid.uuid4())
    assert exc.value.status_code == 403


def test_sso_email_match_returns_link_required_not_auto_link(db, make_school, make_staff):
    # M2: a first-time subject matching an existing email is NOT silently linked; it returns a
    # LinkRequired outcome (no DB write, no session) carrying the single-use link token.
    school = make_school()
    staff = make_staff(school, email="t@oakwood.edu")
    outcome = sso_svc.resolve_or_link(
        db, "google", "google-sub-1", "t@oakwood.edu", True, school.id
    )
    assert isinstance(outcome, sso_svc.LinkRequired)
    assert outcome.link_token and outcome.email == "t@oakwood.edu"
    db.refresh(staff)
    assert staff.sso_subject is None  # NOT auto-linked


def test_sso_confirm_link_binds_subject_then_resolves_by_subject(db, make_school, make_staff):
    school = make_school()
    staff = make_staff(school, email="t@oakwood.edu")
    outcome = sso_svc.resolve_or_link(
        db, "google", "google-sub-1", "t@oakwood.edu", True, school.id
    )
    assert isinstance(outcome, sso_svc.LinkRequired)
    sso_svc.complete_link(db, outcome.link_token)  # explicit confirm binds the subject
    db.refresh(staff)
    assert staff.sso_subject == "google-sub-1"
    # subsequent SSO now resolves by subject to a session (even with a different email claim)
    again = sso_svc.resolve_or_link(db, "google", "google-sub-1", "other@x.edu", True, school.id)
    assert isinstance(again, sso_svc.SessionIssued)


def test_sso_unverified_email_cannot_link(db, make_school, make_staff):
    # M1: an unverified (or absent email_verified) claim must not be enough to LINK.
    school = make_school()
    make_staff(school, email="t@oakwood.edu")
    with pytest.raises(HTTPException) as exc:
        sso_svc.resolve_or_link(db, "google", "sub-x", "t@oakwood.edu", False, school.id)
    assert exc.value.status_code == 401


def test_sso_unknown_email_denied(db, make_school):
    school = make_school()
    with pytest.raises(HTTPException) as exc:
        sso_svc.resolve_or_link(db, "google", "sub-x", "ghost@nowhere.edu", True, school.id)
    assert exc.value.status_code == 401


def test_sso_confirm_link_does_not_resolve_across_schools(db, make_school, make_staff):
    # Same email at two schools (unique only per school). Confirming a link into School B must bind
    # ONLY School B's account and never touch School A's (the B1 cross-tenant fix).
    school_a = make_school(code="AAA")
    school_b = make_school(code="BBB")
    staff_a = make_staff(school_a, email="dual@oakwood.edu")
    staff_b = make_staff(school_b, email="dual@oakwood.edu")
    outcome = sso_svc.resolve_or_link(
        db, "microsoft", "ms-sub-9", "dual@oakwood.edu", True, school_b.id
    )
    assert isinstance(outcome, sso_svc.LinkRequired)
    sso_svc.complete_link(db, outcome.link_token)
    db.refresh(staff_a)
    db.refresh(staff_b)
    assert staff_b.sso_subject == "ms-sub-9"
    assert staff_a.sso_subject is None  # School A's account untouched


def test_staff_dependency_rejects_student_session(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    student_session = AuthSession(
        subject_id=student.id, kind=SessionKind.student, school_id=school.id,
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
    )
    with pytest.raises(HTTPException) as exc:
        get_current_staff(student_session, db)
    assert exc.value.status_code == 403


def test_sso_not_configured_returns_501(client):
    assert client.get("/api/v1/auth/staff/sso/google").status_code == 501


def test_sso_unknown_provider_404(client):
    assert client.get("/api/v1/auth/staff/sso/facebook").status_code == 404
