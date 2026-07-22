"""Compliance / governance models (§13.1): parental consent, data export, immutable audit log."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.domain.enums import DataExportKind, DataExportStatus, ParentalConsentStatus
from src.infrastructure.db import Base


class ParentalConsent(Base):
    __tablename__ = "parental_consents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    student_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("students.id"), nullable=False, index=True
    )
    status: Mapped[ParentalConsentStatus] = mapped_column(
        Enum(ParentalConsentStatus, name="parental_consent_status"),
        nullable=False,
        default=ParentalConsentStatus.pending,
    )
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DataExport(Base):
    __tablename__ = "data_exports"

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
