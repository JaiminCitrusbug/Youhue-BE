"""Roster CSV import business logic (FR-03-01) — staff-only, same-school-only.

A staff member (teacher/support — the roles that own a class roster per the FE nav; leadership and
district are refused here, same as every other school-write route) uploads a CSV of students. A
valid file adds the listed students and age-bands every one of them (5-7 / 8-11 / 12-18) from an
``age`` or ``grade`` column — GATE G-2 downstream literally cannot be reached until this endpoint
has written at least one student, since ``Student.age_band`` is NOT NULL on the model.

Validation order (each stage's failure short-circuits the rest, so a caller never pays for parsing
a file that was already rejected on type or size):
  1. role + same-school authorization                              -> 403
  2. file type (extension AND declared content-type)                -> 415, TRUE reason named
  3. size cap                                                        -> 413
  4. content scan (binary signature / NUL byte / not UTF-8 text)     -> 400
  5. CSV structure (header row, required columns present)           -> 422
  6. row count cap                                                   -> 413
  7. per-row validation (name / age-or-grade / status)               -> 422, every bad row listed
  8. write — one ACID transaction (the router commits once)

CSV contract (header row required, case-insensitive column names, any order):
  * ``name``          (required) — the student's display name.
  * ``age``            (optional int 5-18) OR ``grade`` (optional; K/1..12) — at least one must be
    present PER ROW so it can be age-banded; ``age`` wins when a row has both.
  * ``external_ref``  (optional) — the school's own student identifier (SIS id).
  * ``status``         (optional: ``active`` | ``inactive``, default ``active``).

Idempotency on retry [Baseline BR-05]: a row that carries ``external_ref`` is an UPSERT — a retried
identical file, or a later re-upload of the same roster, updates the existing student in place
rather than creating a duplicate. The application-level select-then-write is backed by the
``uq_students_school_external_ref`` DB partial unique index (migration a1b2c3d4e5f6) so a
genuinely concurrent retry (two requests racing, not the common sequential retry-after-timeout)
cannot both INSERT — the loser fails the whole transaction (rolled back, surfaced as 500) rather
than silently duplicating a student; retrying again then finds the winner's row and updates it.
A row with NO ``external_ref`` has no natural key and is always a CREATE — retrying such a row DOES
create a second student. This is a documented residual, not a silent gap: manufacturing a fuzzy
name-match key was rejected for the same reason FR-02-01's docstring rejects fuzzy school-name
matching (a false match is worse than a duplicate, and this ticket ships no override path); a real
SIS integration supplies a stable external_ref, which is exactly the field this endpoint is built
to key off. Reconciling students ABSENT from a re-import (deactivation) is FR-03-05, not built
here — this endpoint only ever creates/updates rows.

CSV-injection safety: any stored text cell (``display_name`` / ``external_ref``) that starts with
``=``, ``+``, ``-`` or ``@`` is neutralised with a leading apostrophe before it is persisted, so a
spreadsheet that later opens an export of this data never executes it as a formula.

Structured logs: ``fr_03_01_success`` / ``_rejected`` / ``_forbidden`` / ``_error``.
"""
import csv
import io
import logging
import uuid
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.authz import services as authz
from src.application.isolation import services as isolation
from src.constants.enums import StaffRole, StudentAgeBand, StudentStatus
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount
from src.schemas.roster import RosterImportResponse
from src.utils import file_scan

logger = logging.getLogger("youhue.roster")

_FORMULA_PREFIXES = ("=", "+", "-", "@")

_GRADE_TO_AGE: dict[str, int] = {
    "k": 5, "kindergarten": 5,
    "1": 6, "1st": 6, "grade 1": 6,
    "2": 7, "2nd": 7, "grade 2": 7,
    "3": 8, "3rd": 8, "grade 3": 8,
    "4": 9, "4th": 9, "grade 4": 9,
    "5": 10, "5th": 10, "grade 5": 10,
    "6": 11, "6th": 11, "grade 6": 11,
    "7": 12, "7th": 12, "grade 7": 12,
    "8": 13, "8th": 13, "grade 8": 13,
    "9": 14, "9th": 14, "grade 9": 14,
    "10": 15, "10th": 15, "grade 10": 15,
    "11": 16, "11th": 16, "grade 11": 16,
    "12": 17, "12th": 17, "grade 12": 17,
}


@dataclass
class _ParsedRow:
    display_name: str
    age_band: StudentAgeBand
    status: StudentStatus
    external_ref: str | None


def _sanitize_cell(value: str) -> str:
    """Neutralise a formula-injection prefix so the value can never be executed as a formula by a
    spreadsheet that later opens an export of this data (baseline BR-01/BR-05)."""
    if value and value[0] in _FORMULA_PREFIXES:
        return "'" + value
    return value


def _band_for_age(age: int) -> StudentAgeBand | None:
    if 5 <= age <= 7:
        return StudentAgeBand.b5_7
    if 8 <= age <= 11:
        return StudentAgeBand.b8_11
    if 12 <= age <= 18:
        return StudentAgeBand.b12_18
    return None


def _age_from_grade(grade: str) -> int | None:
    return _GRADE_TO_AGE.get(grade.strip().lower())


def _authorize(staff: StaffAccount, school_id: uuid.UUID) -> None:
    try:
        authz.require_roles(staff, StaffRole.teacher, StaffRole.support)
    except HTTPException:
        logger.warning(
            "fr_03_01_forbidden reason=role actor_id=%s role=%s", staff.id, staff.role.value
        )
        raise
    if staff.school_id != school_id:
        logger.warning(
            "fr_03_01_forbidden reason=cross_school actor_id=%s school_id=%s", staff.id, school_id
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")


def _parse_csv(text: str) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames
    if not fieldnames:
        return [], [{"row": 1, "error": "The file has no header row."}]
    normalized_names = {(f or "").strip().lower() for f in fieldnames}
    if "name" not in normalized_names:
        return [], [{"row": 1, "error": "The header row must include a 'name' column."}]
    if "age" not in normalized_names and "grade" not in normalized_names:
        return [], [{"row": 1, "error": "The header row must include an 'age' or 'grade' column."}]

    rows: list[dict[str, str]] = []
    for raw in reader:
        normalized: dict[str, str] = {}
        for key, value in raw.items():
            if key is None:
                continue  # ragged row with more cells than headers -> the overflow is discarded
            normalized[key.strip().lower()] = (value or "").strip()
        rows.append(normalized)
    return rows, []


def _validate_rows(rows: list[dict[str, str]]) -> tuple[list[_ParsedRow], list[dict[str, object]]]:
    parsed: list[_ParsedRow] = []
    errors: list[dict[str, object]] = []
    for i, raw_row in enumerate(rows, start=2):  # row 1 is the header
        name = raw_row.get("name", "")
        if not name:
            errors.append({"row": i, "error": "Missing student name."})
            continue

        age_val = raw_row.get("age", "")
        grade_val = raw_row.get("grade", "")
        age: int | None = None
        if age_val:
            try:
                age = int(age_val)
            except ValueError:
                errors.append({"row": i, "error": f"Age '{age_val}' is not a whole number."})
                continue
        elif grade_val:
            age = _age_from_grade(grade_val)
            if age is None:
                errors.append({"row": i, "error": f"Grade '{grade_val}' is not recognised."})
                continue
        else:
            errors.append({"row": i, "error": "Row has neither an age nor a grade."})
            continue

        band = _band_for_age(age)
        if band is None:
            errors.append({"row": i, "error": f"Age {age} is outside the supported 5-18 range."})
            continue

        status_val = (raw_row.get("status") or "active").lower()
        try:
            row_status = StudentStatus(status_val)
        except ValueError:
            errors.append({"row": i, "error": f"Status '{status_val}' must be active or inactive."})
            continue

        external_ref = raw_row.get("external_ref") or None
        parsed.append(
            _ParsedRow(
                display_name=_sanitize_cell(name),
                age_band=band,
                status=row_status,
                external_ref=_sanitize_cell(external_ref) if external_ref else None,
            )
        )
    return parsed, errors


def import_roster(
    db: Session,
    staff: StaffAccount,
    school_id: uuid.UUID,
    *,
    filename: str,
    content_type: str | None,
    raw: bytes,
) -> RosterImportResponse:
    _authorize(staff, school_id)

    try:
        file_scan.check_file_type(filename, content_type)
    except file_scan.UnsupportedFileTypeError as exc:
        logger.info("fr_03_01_rejected reason=unsupported_type school_id=%s", school_id)
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "code": "unsupported_file_type",
                "message": "The file type is not accepted. Upload a .csv roster file.",
            },
        ) from exc

    try:
        file_scan.check_size(raw, settings.roster_import_max_bytes)
    except file_scan.FileTooLargeError as exc:
        logger.info("fr_03_01_rejected reason=too_large school_id=%s", school_id)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "file_too_large",
                "message": (
                    f"The file is over the {settings.roster_import_max_bytes // 1_000_000}MB limit."
                ),
            },
        ) from exc

    try:
        text = file_scan.scan_and_decode(raw)
    except file_scan.FailedScanError as exc:
        logger.info("fr_03_01_rejected reason=failed_scan school_id=%s", school_id)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={"code": "failed_scan", "message": str(exc)},
        ) from exc

    rows, structural_errors = _parse_csv(text)
    if structural_errors:
        logger.info("fr_03_01_rejected reason=malformed_structure school_id=%s", school_id)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "malformed_rows",
                "message": "The CSV structure is invalid.",
                "errors": structural_errors,
            },
        )

    if len(rows) > settings.roster_import_max_rows:
        logger.info("fr_03_01_rejected reason=too_many_rows school_id=%s", school_id)
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "code": "file_too_large",
                "message": (
                    f"The roster exceeds the {settings.roster_import_max_rows}-student limit."
                ),
            },
        )

    parsed, row_errors = _validate_rows(rows)
    if row_errors:
        logger.info(
            "fr_03_01_rejected reason=malformed_rows school_id=%s count=%d",
            school_id, len(row_errors),
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "malformed_rows",
                "message": "One or more rows could not be imported.",
                "errors": row_errors,
            },
        )

    imported = 0
    banded = 0
    for row in parsed:
        existing = (
            identity_db.get_student_by_external_ref(db, school_id, row.external_ref)
            if row.external_ref
            else None
        )
        if existing is not None:
            existing.display_name = row.display_name
            existing.age_band = row.age_band
            existing.status = row.status
        else:
            identity_db.create_student(
                db,
                school_id=school_id,
                display_name=row.display_name,
                age_band=row.age_band,
                status=row.status,
                external_ref=row.external_ref,
            )
            imported += 1
        banded += 1

    isolation.audit(
        db, actor_id=staff.id, action="roster.imported", target=str(school_id), school_id=school_id
    )
    logger.info(
        "fr_03_01_success school_id=%s actor_id=%s imported=%d banded=%d",
        school_id, staff.id, imported, banded,
    )
    return RosterImportResponse(imported=imported, banded=banded)
