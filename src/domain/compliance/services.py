"""Compliance domain services — audit persistence + scoped loads (DB access only)."""
import uuid
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy.orm import Session

from src.constants.enums import ParentalConsentStatus
from src.domain.compliance.models import AuditLog, ParentalConsent
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
