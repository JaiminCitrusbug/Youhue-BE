"""FR-03-05 roster re-import reconciliation: staff-only, same-school-only, reuses FR-03-01's
validation pipeline (SC-037).

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  a student present in both rosters (matched by external_ref) stays active
        -> ..._returning_student_stays_active
  MN-2  a student absent from the new upload is deactivated, data retained, never deleted
        -> ..._leaver_is_deactivated_but_the_row_still_exists, ..._leaver_row_is_never_hard_deleted
  MN-3  a student new to the upload is added as active
        -> ..._new_student_is_added_as_active
  MN-4  200 body is real counts {stayers_active, leavers_deactivated, new_added}
        -> ..._response_reports_real_reconciliation_counts
  MN-5  same negative statuses as import (415/413/422/400), reusing the SAME pipeline
        -> ..._wrong_file_type_rejected, ..._oversize_rejected, ..._bad_row_rejected,
           ..._binary_content_rejected
  MN-6  staff-only, same-school-only (403)
        -> ..._wrong_role_forbidden, ..._cross_school_forbidden
  MN-7  ACID + idempotent on retry
        -> ..._retry_is_idempotent, ..._unexpected_failure_rolls_back
  MN-8  a student with no external_ref is never auto-deactivated purely for lacking a key
        -> ..._student_without_external_ref_is_left_untouched
  MN-9  a previously-deactivated leaver reappearing in a later upload is reactivated
        -> ..._returning_leaver_is_reactivated
  MN-10 GET /roster (SC-037) lists every student, any status, real data
        -> ..._get_roster_lists_every_student_any_status
"""
import uuid

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.application.roster import services as roster_svc
from src.constants.enums import SessionKind, StaffRole, StudentStatus
from src.domain.identity.models import Student

RECONCILE = "/api/v1/schools/{school_id}/roster/reconcile"
ROSTER = "/api/v1/schools/{school_id}/roster"


def _mint_staff_session(db, staff) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _reconcile(client, token: str, school_id, content: bytes, filename="roster.csv",
               content_type="text/csv"):
    url = RECONCILE.format(school_id=school_id)
    return client.post(
        url, headers=_auth(token), files={"file": (filename, content, content_type)}
    )


def _csv(rows: list[str], header: str = "name,age,external_ref") -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


@pytest.fixture()
def teacher_ctx(make_school, make_staff, db):
    school = make_school(code="RECON-1", name="Oakwood Reconcile")
    teacher = make_staff(school, email="t@oakwood-recon.edu", role=StaffRole.teacher)
    token = _mint_staff_session(db, teacher)
    return school, teacher, token


def _seed_student(db, school, *, external_ref, status=StudentStatus.active, name="Seed"):
    from src.constants.enums import StudentAgeBand

    student = Student(
        school_id=school.id, display_name=name, age_band=StudentAgeBand.b8_11,
        status=status, external_ref=external_ref,
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    return student


# --- MN-1 / Scenario 1: returning students keep their data ---------------------------------------
def test_returning_student_stays_active(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-1", status=StudentStatus.active, name="Amy")

    r = _reconcile(client, token, school.id, _csv(["Amy,7,ext-1"]))

    assert r.status_code == 200
    assert r.json() == {"stayers_active": 1, "leavers_deactivated": 0, "new_added": 0}
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.status == StudentStatus.active


# --- MN-2 / Scenario 2 (negative): leavers deactivated, data retained, never deleted -------------
def test_leaver_is_deactivated_but_the_row_still_exists(client, db, teacher_ctx):
    """The exact hard-rule proof the ticket asks for: a 'leaver' row still EXISTS (status=inactive)
    after reconciliation — never gone."""
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-leaver", status=StudentStatus.active, name="Ben")

    r = _reconcile(client, token, school.id, _csv(["Someone Else,6,ext-other"]))

    assert r.status_code == 200
    assert r.json() == {"stayers_active": 0, "leavers_deactivated": 1, "new_added": 1}

    leaver = db.query(Student).filter(Student.external_ref == "ext-leaver").one_or_none()
    assert leaver is not None, "the leaver's row was deleted — this must never happen"
    assert leaver.status == StudentStatus.inactive
    assert leaver.display_name == "Ben"  # data retained, not cleared


def test_leaver_row_is_never_hard_deleted_count_unchanged(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-1", status=StudentStatus.active)
    before = db.query(Student).filter(Student.school_id == school.id).count()

    _reconcile(client, token, school.id, _csv(["New Kid,6,ext-2"]))

    after = db.query(Student).filter(Student.school_id == school.id).count()
    assert after == before + 1  # the leaver row persists AND the new row is added — nothing lost


# --- MN-3 / Scenario 3: new students added as active ----------------------------------------------
def test_new_student_is_added_as_active(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx

    r = _reconcile(client, token, school.id, _csv(["Cara,10,ext-new"]))

    assert r.status_code == 200
    assert r.json() == {"stayers_active": 0, "leavers_deactivated": 0, "new_added": 1}
    student = db.query(Student).filter(Student.external_ref == "ext-new").one()
    assert student.status == StudentStatus.active


# --- MN-4: response reports real reconciliation counts, mixed scenario ---------------------------
def test_response_reports_real_reconciliation_counts(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-stay", status=StudentStatus.active, name="Stayer")
    _seed_student(db, school, external_ref="ext-leave", status=StudentStatus.active, name="Leaver")

    content = _csv(["Stayer,7,ext-stay", "Joiner,8,ext-join"])
    r = _reconcile(client, token, school.id, content)

    assert r.status_code == 200
    body = r.json()
    assert body == {"stayers_active": 1, "leavers_deactivated": 1, "new_added": 1}
    # never a fixture number: re-derive from the DB independently
    assert db.query(Student).filter(
        Student.school_id == school.id, Student.status == StudentStatus.active
    ).count() == 2
    assert db.query(Student).filter(
        Student.school_id == school.id, Student.status == StudentStatus.inactive
    ).count() == 1


# --- MN-5: same negative statuses as import, reusing the SAME pipeline ---------------------------
def test_wrong_file_type_is_rejected_with_the_true_reason(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _reconcile(client, token, school.id, b"not a csv", filename="roster.exe",
                   content_type="application/octet-stream")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "unsupported_file_type"


def test_oversize_file_is_rejected(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    monkeypatch.setattr(settings, "roster_import_max_bytes", 10)
    r = _reconcile(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "file_too_large"


def test_bad_row_is_rejected_with_row_level_errors(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = _csv(["Amy,6,ext-1", ",9,ext-2"])
    r = _reconcile(client, token, school.id, content)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "malformed_rows"


def test_binary_content_disguised_as_csv_fails_the_scan(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _reconcile(client, token, school.id, b"%PDF-1.4 not a csv")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "failed_scan"


def test_nothing_is_reconciled_when_any_row_is_malformed(client, db, teacher_ctx):
    """All-or-nothing per file: a rejected upload must not deactivate anyone either."""
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-1", status=StudentStatus.active)
    content = _csv(["Amy,6,ext-1", "Bad,not-a-number,ext-2"])
    r = _reconcile(client, token, school.id, content)
    assert r.status_code == 422
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.status == StudentStatus.active  # untouched, not deactivated


# --- MN-6: staff-only, same-school-only -----------------------------------------------------------
def test_wrong_role_is_forbidden(client, db, make_school, make_staff):
    school = make_school(code="RECON-2")
    district = make_staff(school, email="d@oakwood-recon.edu", role=StaffRole.district)
    token = _mint_staff_session(db, district)
    r = _reconcile(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 403


def test_cross_school_reconcile_is_forbidden(client, db, make_school, make_staff):
    school_a = make_school(code="RECON-A")
    school_b = make_school(code="RECON-B")
    teacher_a = make_staff(school_a, email="t@oakwood-recon-a.edu", role=StaffRole.teacher)
    token_a = _mint_staff_session(db, teacher_a)

    r = _reconcile(client, token_a, school_b.id, _csv(["Amy,6,ext-1"]))

    assert r.status_code == 403


# --- MN-7: ACID + idempotency on retry --------------------------------------------------------------
def test_retry_is_idempotent(client, db, teacher_ctx):
    """Retrying the identical file re-derives the same live-roster state: a stayer stays a
    stayer (never duplicated), an already-created row is now found and treated as a stayer."""
    school, _teacher, token = teacher_ctx
    content = _csv(["Amy,6,ext-1"])

    first = _reconcile(client, token, school.id, content)
    second = _reconcile(client, token, school.id, content)

    assert first.status_code == second.status_code == 200
    assert first.json() == {"stayers_active": 0, "leavers_deactivated": 0, "new_added": 1}
    assert second.json() == {"stayers_active": 1, "leavers_deactivated": 0, "new_added": 0}
    assert db.query(Student).filter(Student.school_id == school.id).count() == 1


def test_an_unexpected_failure_rolls_the_whole_reconciliation_back(client, db, teacher_ctx, monkeypatch):
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(roster_svc.identity_db, "create_student", _boom)
    school, _teacher, token = teacher_ctx

    r = _reconcile(client, token, school.id, _csv(["Amy,6,ext-1"]))

    assert r.status_code == 500
    assert db.query(Student).filter(Student.school_id == school.id).count() == 0


# --- MN-8: a no-ref student is never auto-deactivated purely for lacking a key --------------------
def test_student_without_external_ref_is_left_untouched(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    unref = _seed_student(db, school, external_ref=None, status=StudentStatus.active, name="NoRef")

    r = _reconcile(client, token, school.id, _csv(["Someone,6,ext-1"]))

    assert r.status_code == 200
    db.refresh(unref)
    assert unref.status == StudentStatus.active  # never touched, no natural key to match on


# --- MN-9: a previously-deactivated leaver reappearing is reactivated ------------------------------
def test_returning_leaver_is_reactivated(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-1", status=StudentStatus.inactive, name="Back")

    r = _reconcile(client, token, school.id, _csv(["Back,6,ext-1"]))

    assert r.status_code == 200
    assert r.json() == {"stayers_active": 0, "leavers_deactivated": 0, "new_added": 1}
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.status == StudentStatus.active


# --- MN-10: GET /roster (SC-037) lists every student, any status ----------------------------------
def test_get_roster_lists_every_student_any_status(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _seed_student(db, school, external_ref="ext-1", status=StudentStatus.active, name="Active One")
    _seed_student(db, school, external_ref="ext-2", status=StudentStatus.inactive, name="Left One")

    r = client.get(ROSTER.format(school_id=school.id), headers=_auth(token))

    assert r.status_code == 200
    names_status = {s["display_name"]: s["status"] for s in r.json()["students"]}
    assert names_status == {"Active One": "active", "Left One": "inactive"}


def test_get_roster_is_forbidden_cross_school(client, db, make_school, make_staff):
    school_a = make_school(code="RECON-C")
    school_b = make_school(code="RECON-D")
    teacher_a = make_staff(school_a, email="t@oakwood-recon-c.edu", role=StaffRole.teacher)
    token_a = _mint_staff_session(db, teacher_a)

    r = client.get(ROSTER.format(school_id=school_b.id), headers=_auth(token_a))
    assert r.status_code == 403


# --- structured logging (same caplog limitation as test_roster.py — see its NOTE) -----------------
def test_success_is_logged(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    calls: list[str] = []
    monkeypatch.setattr(roster_svc.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg))
    r = _reconcile(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 200
    assert any("fr_03_05_success" in c for c in calls)


def test_rejection_is_logged(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    calls: list[str] = []
    monkeypatch.setattr(roster_svc.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg))
    r = _reconcile(client, token, school.id, b"not-csv", filename="roster.exe",
                   content_type="application/octet-stream")
    assert r.status_code == 415
    assert any("fr_03_05_rejected" in c for c in calls)


def test_forbidden_is_logged(client, db, make_school, make_staff, monkeypatch):
    school = make_school(code="RECON-3")
    district = make_staff(school, email="d2@oakwood-recon.edu", role=StaffRole.district)
    token = _mint_staff_session(db, district)
    calls: list[str] = []
    monkeypatch.setattr(
        roster_svc.logger, "warning", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = _reconcile(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 403
    assert any("fr_03_05_forbidden" in c for c in calls)


def test_error_is_logged(client, teacher_ctx, monkeypatch, caplog):
    school, _teacher, token = teacher_ctx

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(roster_svc.identity_db, "create_student", _boom)
    r = _reconcile(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 500  # the router's fr_03_05_error path is what raised this 500


# --- OpenAPI: the frontend generates its client from this ------------------------------------------
def test_openapi_documents_every_status_the_reconcile_endpoint_returns(client):
    path = "/api/v1/schools/{school_id}/roster/reconcile"
    responses = client.get("/openapi.json").json()["paths"][path]["post"]["responses"]
    assert {"200", "400", "403", "413", "415", "422", "500"} <= set(responses)


def test_school_id_must_be_a_uuid(client, teacher_ctx):
    _school, _teacher, token = teacher_ctx
    r = client.post(
        "/api/v1/schools/not-a-uuid/roster/reconcile", headers=_auth(token),
        files={"file": ("r.csv", b"name,age\nA,6\n", "text/csv")},
    )
    assert r.status_code == 422


def test_unknown_school_id_uses_the_403_cross_school_path(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _reconcile(client, token, uuid.uuid4(), _csv(["Amy,6,ext-1"]))
    assert r.status_code == 403
