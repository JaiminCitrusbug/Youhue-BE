"""Import all ORM models so Base.metadata is complete for Alembic autogenerate."""
from src.infrastructure.models.auth import (
    AuthSession,
    LoginAttempt,
    MfaOtp,
    PasswordResetToken,
)
from src.infrastructure.models.billing import Notification, Subscription
from src.infrastructure.models.checkin import Activity, ActivityEngagement, CheckIn
from src.infrastructure.models.compliance import AuditLog, DataExport, ParentalConsent
from src.infrastructure.models.identity import (
    InternalAdmin,
    School,
    StaffAccount,
    Student,
)
from src.infrastructure.models.org import (
    CalendarConfig,
    ClassGroup,
    ClassMembership,
    Invitation,
    StaffClassAccess,
)
from src.infrastructure.models.risk import (
    AlertDelivery,
    AlertRecipientConfig,
    ConcernWordList,
    Flag,
    FlagEvent,
    SupportiveNote,
)

__all__ = [
    "Activity",
    "ActivityEngagement",
    "AlertDelivery",
    "AlertRecipientConfig",
    "AuditLog",
    "AuthSession",
    "CalendarConfig",
    "CheckIn",
    "ClassGroup",
    "ClassMembership",
    "ConcernWordList",
    "DataExport",
    "Flag",
    "FlagEvent",
    "InternalAdmin",
    "Invitation",
    "LoginAttempt",
    "MfaOtp",
    "Notification",
    "ParentalConsent",
    "PasswordResetToken",
    "School",
    "StaffAccount",
    "StaffClassAccess",
    "Student",
    "Subscription",
    "SupportiveNote",
]
