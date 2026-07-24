"""FR-03-01 roster CSV import: staff-only, same-school-only, server-side validated (SC-036).

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  a valid CSV adds the listed students and records age/grade
        -> ..._valid_csv_adds_students_and_bands_them, ..._age_column_bands_correctly (x3 bands)
  MN-2  GATE G-2: age-banding happens from construction, no student can exist unbanded
        -> ..._every_created_student_has_a_non_null_age_band
  MN-3  a wrong file type is rejected with the TRUE reason (415)
        -> ..._wrong_extension_rejected_with_true_reason, ..._wrong_declared_content_type_rejected
  MN-4  oversize (413), malformed rows (422), failed scan (400) are all rejected server-side
        -> ..._oversize_file_rejected, ..._too_many_rows_rejected, ..._missing_header_rejected,
           ..._bad_row_rejected_with_row_errors, ..._binary_content_rejected_as_failed_scan,
           ..._empty_file_rejected
  MN-5  CSV-injection cells are neutralised before persisting
        -> ..._formula_injection_prefixes_are_neutralised
  MN-6  staff-only, same-school-only (403)
        -> ..._wrong_role_forbidden, ..._cross_school_forbidden
  MN-7  ACID + idempotent on retry [BR-05]
        -> ..._retry_with_external_ref_upserts_not_duplicates, ..._unexpected_failure_rolls_back
"""
import uuid

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.application.roster import services as roster_svc
from src.constants.enums import SessionKind, StaffRole, StudentAgeBand, StudentStatus
from src.domain.identity.models import Student

IMPORT = "/api/v1/schools/{school_id}/roster/import"


def _mint_staff_session(db, staff) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _upload(client, token: str, school_id, content: bytes, filename="roster.csv",
            content_type="text/csv"):
    url = IMPORT.format(school_id=school_id)
    return client.post(
        url, headers=_auth(token), files={"file": (filename, content, content_type)}
    )


def _csv(rows: list[str], header: str = "name,age,external_ref") -> bytes:
    return ("\n".join([header, *rows]) + "\n").encode("utf-8")


@pytest.fixture()
def teacher_ctx(make_school, make_staff, db):
    school = make_school(code="ROSTER-1", name="Oakwood Roster")
    teacher = make_staff(school, email="t@oakwood.edu", role=StaffRole.teacher)
    token = _mint_staff_session(db, teacher)
    return school, teacher, token


# --- MN-1 / Scenario 1+2: valid import adds + bands students -----------------------------------
def test_valid_csv_adds_students_and_bands_them(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = _csv(["Amy,6,ext-1", "Ben,9,ext-2", "Cara,15,ext-3"])

    r = _upload(client, token, school.id, content)

    assert r.status_code == 200
    body = r.json()
    assert body == {"imported": 3, "banded": 3}

    students = db.query(Student).filter(Student.school_id == school.id).all()
    assert len(students) == 3
    by_ref = {s.external_ref: s for s in students}
    assert by_ref["ext-1"].age_band == StudentAgeBand.b5_7
    assert by_ref["ext-2"].age_band == StudentAgeBand.b8_11
    assert by_ref["ext-3"].age_band == StudentAgeBand.b12_18
    assert by_ref["ext-1"].display_name == "Amy"
    assert by_ref["ext-1"].status == StudentStatus.active


@pytest.mark.parametrize(
    "age,expected", [(6, StudentAgeBand.b5_7), (9, StudentAgeBand.b8_11), (15, StudentAgeBand.b12_18)]
)
def test_age_column_bands_correctly(client, db, teacher_ctx, age, expected):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, _csv([f"Kid,{age},ext-1"]))
    assert r.status_code == 200
    student = db.query(Student).filter(Student.school_id == school.id).one()
    assert student.age_band == expected


def test_grade_column_is_accepted_when_age_is_absent(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = b"name,grade,external_ref\nKid,3rd,ext-9\n"
    r = _upload(client, token, school.id, content)
    assert r.status_code == 200
    student = db.query(Student).filter(Student.external_ref == "ext-9").one()
    assert student.age_band == StudentAgeBand.b8_11  # grade 3 -> age 8


# --- MN-2 / GATE G-2: no student can exist unbanded ---------------------------------------------
def test_every_created_student_has_a_non_null_age_band(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    student = db.query(Student).filter(Student.school_id == school.id).one()
    assert student.age_band is not None


# --- MN-3 / Scenario 3: wrong file type rejected with the TRUE reason --------------------------
def test_wrong_extension_is_rejected_with_the_true_reason(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, b"not a csv", filename="roster.exe",
                content_type="application/octet-stream")
    assert r.status_code == 415
    detail = r.json()["detail"]
    assert detail["code"] == "unsupported_file_type"
    assert "not accepted" in detail["message"].lower()


def test_wrong_declared_content_type_is_also_rejected(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, b"name,age\nA,6\n", filename="roster.csv",
                content_type="application/pdf")
    assert r.status_code == 415
    assert r.json()["detail"]["code"] == "unsupported_file_type"


# --- MN-4: oversize / malformed rows / failed scan ----------------------------------------------
def test_oversize_file_is_rejected(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    monkeypatch.setattr(settings, "roster_import_max_bytes", 10)
    r = _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "file_too_large"


def test_too_many_rows_is_rejected(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    monkeypatch.setattr(settings, "roster_import_max_rows", 2)
    rows = [f"Kid{i},6,ext-{i}" for i in range(5)]
    r = _upload(client, token, school.id, _csv(rows))
    assert r.status_code == 413
    assert r.json()["detail"]["code"] == "file_too_large"


def test_header_without_name_column_is_rejected(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, b"age,external_ref\n6,ext-1\n")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "malformed_rows"


def test_bad_row_is_rejected_with_row_level_errors(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = _csv(["Amy,6,ext-1", ",9,ext-2", "Cara,not-a-number,ext-3"])
    r = _upload(client, token, school.id, content)
    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert {e["row"] for e in errors} == {3, 4}  # header is row 1


def test_binary_content_disguised_as_csv_fails_the_scan(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, b"%PDF-1.4 not a csv")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "failed_scan"


def test_empty_file_is_rejected_as_failed_scan(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, b"")
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "failed_scan"


def test_nothing_is_written_when_any_row_is_malformed(client, db, teacher_ctx):
    """All-or-nothing per file: one bad row must not let the good rows through."""
    school, _teacher, token = teacher_ctx
    content = _csv(["Amy,6,ext-1", "Bad,not-a-number,ext-2"])
    r = _upload(client, token, school.id, content)
    assert r.status_code == 422
    assert db.query(Student).filter(Student.school_id == school.id).count() == 0


# --- MN-5: CSV-injection safety ------------------------------------------------------------------
@pytest.mark.parametrize("prefix", ["=", "+", "-", "@"])
def test_formula_injection_prefixes_are_neutralised(client, db, teacher_ctx, prefix):
    school, _teacher, token = teacher_ctx
    name = f"{prefix}cmd|'/c calc'!A1"
    content = f"name,age,external_ref\n{name},6,ext-1\n".encode()
    r = _upload(client, token, school.id, content)
    assert r.status_code == 200
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.display_name.startswith("'")
    assert student.display_name == "'" + name


def test_ordinary_names_are_not_touched(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _upload(client, token, school.id, _csv(["Amy Smith,6,ext-1"]))
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.display_name == "Amy Smith"


# --- MN-6: staff-only, same-school-only ----------------------------------------------------------
def test_wrong_role_is_forbidden(client, db, make_school, make_staff):
    school = make_school(code="ROSTER-2")
    district = make_staff(school, email="d@oakwood.edu", role=StaffRole.district)
    token = _mint_staff_session(db, district)
    r = _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 403
    assert db.query(Student).filter(Student.school_id == school.id).count() == 0


def test_cross_school_upload_is_forbidden(client, db, make_school, make_staff):
    school_a = make_school(code="ROSTER-A")
    school_b = make_school(code="ROSTER-B")
    teacher_a = make_staff(school_a, email="t@oakwood-a.edu", role=StaffRole.teacher)
    token_a = _mint_staff_session(db, teacher_a)

    r = _upload(client, token_a, school_b.id, _csv(["Amy,6,ext-1"]))

    assert r.status_code == 403
    assert db.query(Student).filter(Student.school_id == school_b.id).count() == 0


def test_support_role_is_allowed(client, teacher_ctx, make_staff, db):
    school, _teacher, _token = teacher_ctx
    support = make_staff(school, email="s@oakwood.edu", role=StaffRole.support)
    token = _mint_staff_session(db, support)
    r = _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 200


# --- MN-7: ACID + idempotency on retry [BR-05] ---------------------------------------------------
def test_retry_with_external_ref_upserts_not_duplicates(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = _csv(["Amy,6,ext-1"])

    first = _upload(client, token, school.id, content)
    second = _upload(client, token, school.id, content)

    assert first.status_code == second.status_code == 200
    assert first.json() == {"imported": 1, "banded": 1}
    assert second.json() == {"imported": 0, "banded": 1}  # updated in place, not re-created
    assert db.query(Student).filter(Student.school_id == school.id).count() == 1


def test_retry_can_change_the_banded_age(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    _upload(client, token, school.id, _csv(["Amy,15,ext-1"]))
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.age_band == StudentAgeBand.b12_18


def test_an_unexpected_failure_rolls_the_whole_import_back(client, db, teacher_ctx, monkeypatch):
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("db went away")

    monkeypatch.setattr(roster_svc.identity_db, "create_student", _boom)
    school, _teacher, token = teacher_ctx

    r = _upload(client, token, school.id, _csv(["Amy,6,ext-1", "Ben,9,ext-2"]))

    assert r.status_code == 500
    assert db.query(Student).filter(Student.school_id == school.id).count() == 0


def test_concurrent_external_ref_insert_is_refused_by_the_database(db, teacher_ctx):
    """The DB backstop (`uq_students_school_external_ref`) for a genuine race the application-level
    select-then-write cannot close on its own — mirrors `test_duplicate_name_is_refused_by_the_database`
    in test_schools.py."""
    from sqlalchemy.exc import IntegrityError

    school, _teacher, _token = teacher_ctx
    db.add(Student(school_id=school.id, display_name="Amy", age_band=StudentAgeBand.b5_7,
                    external_ref="ext-1"))
    db.commit()
    db.add(Student(school_id=school.id, display_name="Amy Two", age_band=StudentAgeBand.b8_11,
                    external_ref="ext-1"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# --- structured logging --------------------------------------------------------------------------
# NOTE: `caplog` cannot be used here — a pre-existing test-harness quirk (`alembic/env.py` calls
# `logging.config.fileConfig(alembic.ini)` from the session-scoped `_schema` fixture; its default
# `disable_existing_loggers=True` silently disables every module logger created at collection time
# that isn't in alembic.ini's `[loggers]` list, "youhue.*" included) means NO caplog-based log
# assertion fires anywhere in this suite, not just here. Out of this ticket's scope to fix (shared
# conftest/alembic infra other lanes depend on) — flagged in the ticket report. Assert the exact
# call instead, which proves the same thing without depending on the broken propagation path.
def test_success_is_logged(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    calls: list[str] = []
    monkeypatch.setattr(roster_svc.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg))
    r = _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 200
    assert any("fr_03_01_success" in c for c in calls)


def test_rejection_is_logged(client, teacher_ctx, monkeypatch):
    school, _teacher, token = teacher_ctx
    calls: list[str] = []
    monkeypatch.setattr(roster_svc.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg))
    r = _upload(client, token, school.id, b"not-csv", filename="roster.exe",
                content_type="application/octet-stream")
    assert r.status_code == 415
    assert any("fr_03_01_rejected" in c for c in calls)


def test_forbidden_is_logged(client, db, make_school, make_staff, monkeypatch):
    school = make_school(code="ROSTER-3")
    district = make_staff(school, email="d2@oakwood.edu", role=StaffRole.district)
    token = _mint_staff_session(db, district)
    calls: list[str] = []
    monkeypatch.setattr(
        roster_svc.logger, "warning", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = _upload(client, token, school.id, _csv(["Amy,6,ext-1"]))
    assert r.status_code == 403
    assert any("fr_03_01_forbidden" in c for c in calls)


# --- OpenAPI: the frontend generates its client from this ----------------------------------------
def test_openapi_documents_every_status_the_endpoint_returns(client):
    path = "/api/v1/schools/{school_id}/roster/import"
    responses = client.get("/openapi.json").json()["paths"][path]["post"]["responses"]
    assert {"200", "400", "403", "413", "415", "422", "500"} <= set(responses)


def test_school_id_must_be_a_uuid(client, teacher_ctx):
    _school, _teacher, token = teacher_ctx
    r = client.post(
        "/api/v1/schools/not-a-uuid/roster/import", headers=_auth(token),
        files={"file": ("r.csv", b"name,age\nA,6\n", "text/csv")},
    )
    assert r.status_code == 422


def test_unknown_column_row_still_requires_name(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, school.id, b"name,age,extra_col\n,6,x\n")
    assert r.status_code == 422


def test_status_column_controls_active_inactive(client, db, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = b"name,age,status,external_ref\nAmy,6,inactive,ext-1\n"
    r = _upload(client, token, school.id, content)
    assert r.status_code == 200
    student = db.query(Student).filter(Student.external_ref == "ext-1").one()
    assert student.status == StudentStatus.inactive


def test_invalid_status_value_is_a_malformed_row(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = b"name,age,status,external_ref\nAmy,6,bogus,ext-1\n"
    r = _upload(client, token, school.id, content)
    assert r.status_code == 422


def test_age_out_of_supported_range_is_a_malformed_row(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = _csv(["Toddler,3,ext-1"])
    r = _upload(client, token, school.id, content)
    assert r.status_code == 422


def test_row_with_no_age_or_grade_is_a_malformed_row(client, teacher_ctx):
    school, _teacher, token = teacher_ctx
    content = b"name,age,grade,external_ref\nAmy,,,ext-1\n"
    r = _upload(client, token, school.id, content)
    assert r.status_code == 422


def test_row_without_external_ref_is_always_a_create(client, db, teacher_ctx):
    """Documented residual: no natural key means a retry of a no-ref row creates a second student."""
    school, _teacher, token = teacher_ctx
    content = b"name,age\nAmy,6\n"
    _upload(client, token, school.id, content)
    _upload(client, token, school.id, content)
    assert db.query(Student).filter(Student.school_id == school.id).count() == 2


def test_unknown_school_id_uses_the_403_cross_school_path(client, teacher_ctx):
    """No separate 404 branch exists for roster import: a StaffDep caller whose school_id does not
    match the path is refused the same way whether the id belongs to another real school or none
    at all — never leaking whether a given school id exists."""
    school, _teacher, token = teacher_ctx
    r = _upload(client, token, uuid.uuid4(), _csv(["Amy,6,ext-1"]))
    assert r.status_code == 403
