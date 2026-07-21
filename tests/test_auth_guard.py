"""Cross-tenant guard (FR-01-07), SSO linked-identity, and student-cannot-be-staff (INFRA-01)."""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from src.application.auth import sso as sso_svc
from src.domain.enums import SessionKind
from src.infrastructure.models.auth import AuthSession
from src.interfaces.deps import get_current_staff, require_same_school


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


def test_sso_first_match_links_existing_email(db, make_school, make_staff):
    staff = make_staff(make_school(), email="t@oakwood.edu")
    resolved = sso_svc.resolve_or_link(db, subject="google-sub-1", email="t@oakwood.edu")
    assert resolved.id == staff.id
    assert resolved.sso_subject == "google-sub-1"
    # subsequent SSO resolves by subject (even with a different email claim)
    again = sso_svc.resolve_or_link(db, subject="google-sub-1", email="other@x.edu")
    assert again.id == staff.id


def test_sso_unknown_email_denied(db, make_school):
    make_school()
    with pytest.raises(HTTPException) as exc:
        sso_svc.resolve_or_link(db, subject="sub-x", email="ghost@nowhere.edu")
    assert exc.value.status_code == 401


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
