"""Import all ORM models so Base.metadata is complete for Alembic autogenerate."""
from src.infrastructure.models.auth import (
    AuthSession,
    LoginAttempt,
    MfaOtp,
    PasswordResetToken,
)
from src.infrastructure.models.identity import (
    InternalAdmin,
    School,
    StaffAccount,
    Student,
)

__all__ = [
    "AuthSession",
    "InternalAdmin",
    "LoginAttempt",
    "MfaOtp",
    "PasswordResetToken",
    "School",
    "StaffAccount",
    "Student",
]
