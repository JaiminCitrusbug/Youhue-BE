"""Identity models (SRS §13.1 subset needed for auth). School is the root tenant.

INFRA-01 owns these because auth cannot exist without them; INFRA-02 extends the model with
the remaining §13.1 entities (classes, check-ins, flags, ...) and the isolation query layer.
`password_hash` / `sso_subject` / `sign_in_code` are auth-infra fields (not domain data).
Internal admins are a SEPARATE platform-level account (owner decision), never a school StaffAccount.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.domain.enums import (
    AuthProvider,
    SchoolStatus,
    SchoolTier,
    StaffRole,
    StaffStatus,
    StudentAgeBand,
    StudentStatus,
)
from src.infrastructure.db import Base


class School(Base):
    __tablename__ = "schools"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[SchoolStatus] = mapped_column(
        Enum(SchoolStatus, name="school_status"), nullable=False, default=SchoolStatus.pending
    )
    tier: Mapped[SchoolTier] = mapped_column(
        Enum(SchoolTier, name="school_tier"), nullable=False, default=SchoolTier.free
    )
    timezone: Mapped[str] = mapped_column(String, nullable=False, default="UTC")
    # auth-infra: short code students enter at sign-in (class codes come in INFRA-02)
    sign_in_code: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class StaffAccount(Base):
    __tablename__ = "staff_accounts"
    __table_args__ = (UniqueConstraint("school_id", "email", name="uq_staff_school_email"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # null for SSO-only
    sso_subject: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    auth_provider: Mapped[AuthProvider] = mapped_column(
        Enum(AuthProvider, name="auth_provider"), nullable=False, default=AuthProvider.password
    )
    role: Mapped[StaffRole] = mapped_column(Enum(StaffRole, name="staff_role"), nullable=False)
    status: Mapped[StaffStatus] = mapped_column(
        Enum(StaffStatus, name="staff_status"), nullable=False, default=StaffStatus.invited
    )
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Student(Base):
    __tablename__ = "students"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    school_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("schools.id"), nullable=False, index=True
    )
    external_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    age_band: Mapped[StudentAgeBand] = mapped_column(
        Enum(StudentAgeBand, name="student_age_band"), nullable=False
    )
    status: Mapped[StudentStatus] = mapped_column(
        Enum(StudentStatus, name="student_status"), nullable=False, default=StudentStatus.active
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class InternalAdmin(Base):
    """Platform-level internal admin — NOT school-scoped. Every action is audit-logged."""

    __tablename__ = "internal_admins"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
