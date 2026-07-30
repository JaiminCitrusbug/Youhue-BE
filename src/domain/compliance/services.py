"""Compliance domain services — audit persistence + scoped loads (DB access only)."""
import uuid
from datetime import UTC, datetime
from typing import TypeVar

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.orm import Session

from src.constants.enums import DataExportKind, DataExportStatus, ParentalConsentStatus
from src.domain.auth.models import AuthSession, MfaOtp, PasswordResetToken
from src.domain.billing.models import Notification, Subscription
from src.domain.checkin.models import Activity, ActivityEngagement, CheckIn, CheckInSettings
from src.domain.compliance.models import AuditLog, DataExport, ParentalConsent
from src.domain.identity.models import School, StaffAccount, Student
from src.domain.org.models import (
    CalendarConfig,
    ClassGroup,
    ClassMembership,
    Invitation,
    StaffClassAccess,
)
from src.domain.risk.models import (
    AlertDelivery,
    AlertRecipientConfig,
    ConcernWordList,
    Flag,
    FlagEvent,
    SupportiveNote,
)

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


def get_export_and_delete(db: Session, school_id: uuid.UUID) -> DataExport | None:
    """The single (DB-enforced unique, `uq_data_exports_school_export_and_delete`)
    export_and_delete-kind row for a school, if its exit flow has been started (FR-20-02)."""
    return db.scalar(
        select(DataExport).where(
            DataExport.school_id == school_id, DataExport.kind == DataExportKind.export_and_delete
        )
    )


def hard_delete_school_cascade(db: Session, school_id: uuid.UUID) -> None:
    """FR-20-02 (SC-065): irreversible cascade hard-delete of everything reachable from ONE school —
    the caller MUST have already confirmed the exit export is `ready` (export precedes delete).

    No FK on `schools.id` anywhere in this schema carries `ondelete=CASCADE` (by design — an
    accidental delete must not silently fan out on its own), so every dependent row is removed here
    explicitly, leaves-first, inside the caller's ACID transaction (one commit/rollback for the
    whole exit — BR-05). Exceptions, all deliberate:
      - `AuditLog.school_id` is nullable and IS the durable trail BR-12 requires to outlive the
        school it describes — its rows are re-pointed to NULL, never deleted.
      - `FlagEvent` and `AuditLog` both carry a DB-level `BEFORE UPDATE OR DELETE` append-only
        trigger (migration `c0ffee000001`, `youhue_block_mutation()`) — a deliberate guard against
        an ordinary write ever tampering with either trail. A genuine right-to-erasure hard-delete
        is not an ordinary write: it is the one privileged, explicit operation this guard is not
        meant to block. Postgres re-fires row triggers even for a caller with superuser-adjacent
        DB privileges, so the only way through is to name the exemption — `ALTER TABLE ... DISABLE
        TRIGGER` for the exact statement that needs it, `ENABLE TRIGGER` again immediately after,
        both inside this SAME transaction (a rollback restores the trigger; nothing observes it
        "off" outside this function — `test_hard_delete_removes_every_school_scoped_row` and
        `test_append_only_triggers_still_enforced_after_a_hard_delete` in
        `tests/test_data_deletion.py` prove both halves).
      - `LoginAttempt` is left untouched: it is keyed by email `identifier` only, and
        `StaffAccount` explicitly allows the SAME email to hold accounts at more than one school
        (see its model docstring) — matching by email risks touching another school's rows, which
        FR-20-07 forbids. There is no safe, id-scoped way to purge it here.
    Auth-infra (`AuthSession` / `MfaOtp` / `PasswordResetToken`) carries no DB-level FK to
    schools/staff/students, so it cannot block this delete — it is purged anyway, scoped by exact
    id (never by email), so "nothing lingers" also holds for sign-in plumbing.
    """
    student_ids = select(Student.id).where(Student.school_id == school_id)
    staff_ids = select(StaffAccount.id).where(StaffAccount.school_id == school_id)
    class_ids = select(ClassGroup.id).where(ClassGroup.school_id == school_id)
    flag_ids = select(Flag.id).where(Flag.school_id == school_id)
    _sync_off = {"synchronize_session": False}

    # ---- leaves: rows with no further dependents ---------------------------------------------
    db.execute(
        delete(AlertDelivery)
        .where(or_(AlertDelivery.recipient_id.in_(staff_ids), AlertDelivery.flag_id.in_(flag_ids)))
        .execution_options(**_sync_off)
    )
    # FlagEvent is append-only at the DB level (BEFORE DELETE trigger, c0ffee000001) — a genuine
    # hard-delete is the one privileged exemption; disable the trigger for exactly this statement,
    # re-enable it immediately after, both inside this same transaction.
    db.execute(text("ALTER TABLE flag_events DISABLE TRIGGER flag_events_append_only"))
    db.execute(
        delete(FlagEvent)
        .where(or_(FlagEvent.flag_id.in_(flag_ids), FlagEvent.actor_id.in_(staff_ids)))
        .execution_options(**_sync_off)
    )
    db.execute(text("ALTER TABLE flag_events ENABLE TRIGGER flag_events_append_only"))
    db.execute(
        delete(SupportiveNote)
        .where(
            or_(SupportiveNote.student_id.in_(student_ids), SupportiveNote.sender_id.in_(staff_ids))
        )
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(ActivityEngagement)
        .where(ActivityEngagement.student_id.in_(student_ids))
        .execution_options(**_sync_off)
    )

    # ---- direct school-scoped rows (dependents cleared above) --------------------------------
    db.execute(delete(Flag).where(Flag.school_id == school_id).execution_options(**_sync_off))
    db.execute(
        delete(Notification)
        .where(Notification.recipient_id.in_(staff_ids))
        .execution_options(**_sync_off)
    )
    db.execute(delete(CheckIn).where(CheckIn.school_id == school_id).execution_options(**_sync_off))
    db.execute(
        delete(ClassMembership)
        .where(
            or_(
                ClassMembership.student_id.in_(student_ids),
                ClassMembership.class_id.in_(class_ids),
            )
        )
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(StaffClassAccess)
        .where(
            or_(
                StaffClassAccess.staff_id.in_(staff_ids),
                StaffClassAccess.class_id.in_(class_ids),
            )
        )
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(ParentalConsent)
        .where(ParentalConsent.student_id.in_(student_ids))
        .execution_options(**_sync_off)
    )
    # includes the export_and_delete row that gated this very call — it is "the school's data" too,
    # and cannot outlive the school (its school_id FK is NOT NULL). The already-exported artifact in
    # object storage is untouched (that copy is the whole point of exporting first).
    db.execute(
        delete(DataExport).where(DataExport.school_id == school_id).execution_options(**_sync_off)
    )
    db.execute(
        delete(Invitation).where(Invitation.school_id == school_id).execution_options(**_sync_off)
    )
    db.execute(
        delete(CheckInSettings)
        .where(CheckInSettings.school_id == school_id)
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(CalendarConfig)
        .where(CalendarConfig.school_id == school_id)
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(Subscription)
        .where(Subscription.school_id == school_id)
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(AlertRecipientConfig)
        .where(AlertRecipientConfig.school_id == school_id)
        .execution_options(**_sync_off)
    )
    # school_id is nullable on ConcernWordList (NULL = the shared platform default) — `==` never
    # matches NULL, so the platform default row is never touched, only this school's own override.
    db.execute(
        delete(ConcernWordList)
        .where(ConcernWordList.school_id == school_id)
        .execution_options(**_sync_off)
    )

    # ---- second-order school-scoped rows ------------------------------------------------------
    db.execute(
        delete(ClassGroup).where(ClassGroup.school_id == school_id).execution_options(**_sync_off)
    )
    # school_id is nullable on Activity too (NULL = shared seed content) — same NULL-never-matches
    # guard keeps every platform seed activity untouched; only this school's OWN activities go.
    db.execute(
        delete(Activity).where(Activity.school_id == school_id).execution_options(**_sync_off)
    )

    # ---- auth-infra cleanup (no DB FK; purged for "nothing lingers", not to avoid a violation) --
    db.execute(
        delete(AuthSession)
        .where(
            or_(
                AuthSession.school_id == school_id,
                AuthSession.subject_id.in_(student_ids),
                AuthSession.subject_id.in_(staff_ids),
            )
        )
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(MfaOtp).where(MfaOtp.subject_id.in_(staff_ids)).execution_options(**_sync_off)
    )
    db.execute(
        delete(PasswordResetToken)
        .where(PasswordResetToken.staff_id.in_(staff_ids))
        .execution_options(**_sync_off)
    )

    # ---- identity roots ------------------------------------------------------------------------
    db.execute(
        delete(StaffAccount)
        .where(StaffAccount.school_id == school_id)
        .execution_options(**_sync_off)
    )
    db.execute(
        delete(Student).where(Student.school_id == school_id).execution_options(**_sync_off)
    )

    # ---- immutable audit trail: detach, never delete (BR-12) -----------------------------------
    # AuditLog is ALSO append-only at the DB level (BEFORE UPDATE trigger, c0ffee000001) — same
    # named, scoped, immediately-reverted exemption as FlagEvent above.
    db.execute(text("ALTER TABLE audit_logs DISABLE TRIGGER audit_logs_append_only"))
    db.execute(
        update(AuditLog)
        .where(AuditLog.school_id == school_id)
        .values(school_id=None)
        .execution_options(**_sync_off)
    )
    db.execute(text("ALTER TABLE audit_logs ENABLE TRIGGER audit_logs_append_only"))

    # ---- the school itself ----------------------------------------------------------------------
    db.execute(delete(School).where(School.id == school_id).execution_options(**_sync_off))
    db.flush()
