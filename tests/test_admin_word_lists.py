"""FR-19-05 — admin maintains the platform DEFAULT concern-word list.

Covers @FR-19-05:
  Scenario 1 (positive): updating the default applies to schools that have NOT overridden it.
  Scenario 2 (NEG / GATE G-6): a school override takes precedence — changing the default never
    overwrites or reaches into an existing school override.
Plus: RBAC (`manage_word_lists`) 403 + audit for a limited role, server-side validation, and an
idempotent (single-row, retry-safe) upsert.
"""
import pytest

import src.application.auth.admin as admin_mod
from src.application.concern_words import services as cw_svc
from src.application.risk import services as risk
from src.constants.enums import AdminRole
from src.domain.compliance.models import AuditLog
from src.domain.risk import services as risk_db
from src.domain.risk.models import ConcernWordList

ENDPOINT = "/api/v1/admin/concern-words/default"
SIGNIN = "/api/v1/admin/sign-in"


def _complete_signin(client, monkeypatch, email="admin@youhue.app", password="Password123"):
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        admin_mod, "send_email", lambda to, subject, body: captured.update(body=body)
    )
    client.post(SIGNIN, json={"email": email, "password": password})
    code = captured["body"].split()[-1]
    return client.post(
        SIGNIN, json={"email": email, "password": password, "mfa_code": code}
    ).json()["admin_session"]


def _mk_checkin(db, student, school, reflection):
    from datetime import UTC, datetime

    from src.domain.checkin.models import CheckIn

    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=3,
        reflection_text=reflection, submitted_at=datetime.now(UTC),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


# ---- Scenario 1: update the default (positive) -----------------------------

def test_update_default_applies_to_schools_without_override(db, make_admin, make_school, make_student):
    admin = make_admin(role=AdminRole.superadmin)
    cw_svc.update_default(db, admin, ["mango"])  # a novel word not in the env seed
    db.commit()
    school = make_school()  # no override -> falls back to the platform default
    student = make_student(school)
    c = _mk_checkin(db, student, school, "i want a mango")
    assert "mango" in risk.score_checkin(db, c).matched_terms


def test_endpoint_updates_default_and_normalizes(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    token = _complete_signin(client, monkeypatch)
    r = client.put(
        ENDPOINT, json={"words": ["  Alone ", "alone", "HOPELESS", ""]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_default"] is True
    assert body["words"] == ["alone", "hopeless"]  # trimmed, lowercased, de-duped, blanks dropped
    assert body["count"] == 2
    assert risk_db.get_default_concern_word_list(db).words == ["alone", "hopeless"]


# ---- Scenario 2: a school override takes precedence (NEG / GATE G-6) --------

def test_school_override_unaffected_by_default_change(db, make_admin, make_school, make_student):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    db.add(ConcernWordList(school_id=school.id, words=["banana"], is_default=False))
    db.commit()
    # the internal team changes the DEFAULT
    cw_svc.update_default(db, admin, ["mango"])
    db.commit()
    # the school's override is byte-for-byte untouched (never overwritten / reached into)
    override = risk_db.get_concern_word_list(db, school.id)
    assert override.words == ["banana"]
    assert override.is_default is False
    # and the school continues to score against ITS override, not the new default
    student = make_student(school)
    assert risk.score_checkin(db, _mk_checkin(db, student, school, "a banana")).flagged
    assert not risk.score_checkin(db, _mk_checkin(db, student, school, "a mango")).flagged


def test_default_write_never_creates_or_touches_override_rows(db, make_admin, make_school):
    admin = make_admin(role=AdminRole.superadmin)
    school = make_school()
    db.add(ConcernWordList(school_id=school.id, words=["banana"], is_default=False))
    db.commit()
    cw_svc.update_default(db, admin, ["mango"])
    db.commit()
    # exactly two rows: one override (school) + one default (platform) — the write added no more
    rows = db.query(ConcernWordList).all()
    assert len(rows) == 2
    assert {r.is_default for r in rows} == {True, False}


# ---- RBAC: a limited role is denied 403 (audit-logged) ---------------------

@pytest.mark.authz
def test_limited_role_denied_and_audit_logged(db, make_admin):
    support = make_admin(email="support@youhue.app", role=AdminRole.support)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        cw_svc.update_default(db, support, ["mango"])
    assert exc.value.status_code == 403
    db.commit()
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "admin.rbac.denied:manage_word_lists")
        .all()
    )
    assert len(rows) == 1 and rows[0].target == "admin_console" and rows[0].school_id is None


@pytest.mark.authz
def test_endpoint_support_role_forbidden(client, make_admin, monkeypatch):
    make_admin(email="support@youhue.app", role=AdminRole.support)
    token = _complete_signin(client, monkeypatch, email="support@youhue.app")
    r = client.put(
        ENDPOINT, json={"words": ["mango"]}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


def test_endpoint_requires_admin_session(client):
    assert client.put(ENDPOINT, json={"words": ["mango"]}).status_code == 403


# ---- validation + idempotency ----------------------------------------------

def test_empty_default_rejected(db, make_admin):
    admin = make_admin(role=AdminRole.superadmin)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        cw_svc.update_default(db, admin, ["  ", ""])  # normalizes to empty
    assert exc.value.status_code == 422


def test_overlong_entry_rejected(db, make_admin):
    admin = make_admin(role=AdminRole.superadmin)
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        cw_svc.update_default(db, admin, ["x" * 101])
    assert exc.value.status_code == 422


def test_update_is_idempotent_single_row(db, make_admin):
    admin = make_admin(role=AdminRole.superadmin)
    cw_svc.update_default(db, admin, ["mango"])
    db.commit()
    cw_svc.update_default(db, admin, ["mango"])  # retry / re-apply
    db.commit()
    assert db.query(ConcernWordList).filter(ConcernWordList.is_default.is_(True)).count() == 1


def test_edit_replaces_the_default_set(db, make_admin):
    admin = make_admin(role=AdminRole.superadmin)
    cw_svc.update_default(db, admin, ["mango", "kiwi"])
    db.commit()
    cw_svc.update_default(db, admin, ["kiwi"])  # remove "mango"
    db.commit()
    assert risk_db.get_default_concern_word_list(db).words == ["kiwi"]
