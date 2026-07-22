"""Imports every domain model so Base.metadata is complete (Alembic autogenerate + create_all)."""
from src.domain.auth.models import (  # noqa: F401
    AuthSession,
    LoginAttempt,
    MfaOtp,
    PasswordResetToken,
)
from src.domain.billing.models import Notification, Subscription  # noqa: F401
from src.domain.checkin.models import Activity, ActivityEngagement, CheckIn  # noqa: F401
from src.domain.compliance.models import AuditLog, DataExport, ParentalConsent  # noqa: F401
from src.domain.identity.models import (  # noqa: F401
    InternalAdmin,
    School,
    StaffAccount,
    Student,
)
from src.domain.org.models import (  # noqa: F401
    CalendarConfig,
    ClassGroup,
    ClassMembership,
    Invitation,
    StaffClassAccess,
)
from src.domain.risk.models import (  # noqa: F401
    AlertDelivery,
    AlertRecipientConfig,
    ConcernWordList,
    Flag,
    FlagEvent,
    SupportiveNote,
)
