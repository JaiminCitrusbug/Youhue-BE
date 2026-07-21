"""Auth-infra tables (Postgres-only, no Redis — owner decision).

Sessions back token revocation + student single-active-device; login_attempts back lockout;
mfa_otps back email-OTP MFA; password_reset_tokens back the single-use expiring reset link.
None of these are §13.1 domain entities — they are auth plumbing.
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from src.domain.enums import SessionKind
from src.infrastructure.db import Base


class AuthSession(Base):
    """One row per issued session; its id is the JWT `jti`. Revoked/expired => token rejected."""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    kind: Mapped[SessionKind] = mapped_column(
        Enum(SessionKind, name="session_kind"), nullable=False
    )
    school_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)  # null = admin
    device_id: Mapped[str | None] = mapped_column(String, nullable=True)  # single-device
    mfa_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LoginAttempt(Base):
    """Append-only sign-in attempts; the lockout window counts recent failures per identifier."""

    __tablename__ = "login_attempts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    identifier: Mapped[str] = mapped_column(String, nullable=False, index=True)  # email
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class MfaOtp(Base):
    """Email-OTP challenge (email OTP only). Code stored hashed, single-use, expiring."""

    __tablename__ = "mfa_otps"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PasswordResetToken(Base):
    """Single-use, expiring reset link (email accounts only). Token stored hashed."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    staff_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
