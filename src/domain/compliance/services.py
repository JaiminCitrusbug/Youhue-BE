"""Compliance domain services — audit persistence + scoped loads (DB access only)."""
import uuid
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy.orm import Session

from src.constants.enums import DataExportKind, DataExportStatus, ParentalConsentStatus
from src.domain.compliance.models import AuditLog, DataExport, ParentalConsent
from src.domain.identity.models import Student

T = TypeVar("T")


def write_audit(
    db: Session,
    *,
    actor_id: uuid.UUID,
    action: str,
    target: str,
    school_id: uuid.UUID | None,
) -> None:
    db.add(AuditLog(actor_id=actor_id, action=action, target=target, school_id=school_id))
    db.flush()


def get_by_id(db: Session, model: type[T], obj_id: uuid.UUID) -> T | None:
    return db.get(model, obj_id)


def get_student(db: Session, student_id: uuid.UUID) -> Student | None:
    return db.get(Student, student_id)


def get_consent(db: Session, student_id: uuid.UUID) -> ParentalConsent | None:
    """The ONE consent record for a student (`ix_parental_consents_student_id` is UNIQUE — FR-20-06
    migration f20606a1b2c3 — so this can never return more than one row)."""
    return (
        db.query(ParentalConsent).filter(ParentalConsent.student_id == student_id).one_or_none()
    )


def upsert_consent(
    db: Session, *, student_id: uuid.UUID, consent_status: ParentalConsentStatus
) -> ParentalConsent:
    """Write the single per-student consent record (insert on first capture, else update in place —
    ACID + idempotent-on-retry per ticket Baseline BR-05). `captured_at` stamps the moment THIS
    capture happened, whatever the resulting status."""
    row = get_consent(db, student_id)
    now = datetime.now(UTC)
    if row is None:
        row = ParentalConsent(student_id=student_id, status=consent_status, captured_at=now)
        db.add(row)
    else:
        row.status = consent_status
        row.captured_at = now
    db.flush()
    return row


def create_export(
    db: Session, *, school_id: uuid.UUID, requested_by: uuid.UUID, kind: DataExportKind
) -> DataExport:
    row = DataExport(
        school_id=school_id, requested_by=requested_by, kind=kind, status=DataExportStatus.pending
    )
    db.add(row)
    db.flush()
    return row


def get_export(db: Session, export_id: uuid.UUID) -> DataExport | None:
    return db.get(DataExport, export_id)


def mark_export_ready(db: Session, export: DataExport, storage_key: str) -> DataExport:
    export.storage_key = storage_key
    export.status = DataExportStatus.ready
    db.flush()
    return export
