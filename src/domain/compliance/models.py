"""Compliance / governance models (§13.1): parental consent, data export, immutable audit log."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from config.db_connection import Base
from src.constants.enums import DataExportKind, DataExportStatus, ParentalConsentStatus


class ParentalConsent(Base):
    __tablename__ = "parental_consents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # FR-20-06 (f20606a1b2c3): UNIQUE — one consent record per student (ticket §Data model).
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True, unique=True
    )
    status: Mapped[ParentalConsentStatus] = mapped_column(
        Enum(ParentalConsentStatus, name="parental_consent_status"),
        nullable=False,
        default=ParentalConsentStatus.pending,
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DataExport(Base):
    __tablename__ = "data_exports"
    __table_args__ = (
        # FR-20-02 (SC-065): at most ONE export_and_delete-kind row per school, ever — the school
        # exit flow's own DB-level idempotency/race backstop (BR-05), the same partial-unique-index
        # shape as uq_flags_open_student_type (FR-12-03) / uq_concern_word_lists_default (FR-19-05).
        # Two concurrent "start the exit" calls for the same school must not both insert a fresh
        # row — that would let two callers each believe THEY are driving a single-shot, irreversible
        # delete. Plain `export`-kind rows (FR-20-01) are unrestricted — a school may request as
        # many routine exports as it likes.
        Index(
            "uq_data_exports_school_export_and_delete",
            "school_id",
            unique=True,
            postgresql_where=text("kind = 'export_and_delete'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_accounts.id"), nullable=False)
    kind: Mapped[DataExportKind] = mapped_column(
        Enum(DataExportKind, name="data_export_kind"), nullable=False
    )
    status: Mapped[DataExportStatus] = mapped_column(
        Enum(DataExportStatus, name="data_export_status"),
        nullable=False,
        default=DataExportStatus.pending,
    )
    # FR-20-01: the object-storage key of the finished artifact — unset while pending, set the
    # moment the artifact is durably written and status flips to ready (migration a7e9c1f34b56).
    storage_key: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AuditLog(Base):
    """System-wide immutable audit of sensitive/admin actions. school_id null = platform-level."""

    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("schools.id"), nullable=True, index=True
    )
    actor_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)
    target: Mapped[str] = mapped_column(String, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
