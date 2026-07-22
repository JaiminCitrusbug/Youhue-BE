"""Canonical named enums (SRS §13.4 state machines). Identity/auth subset lands with INFRA-01;
the remaining domain enums (check-in, flag, subscription, ...) are added by INFRA-02."""
import enum


class SchoolStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    rejected = "rejected"


class SchoolTier(str, enum.Enum):
    free = "free"
    premium = "premium"


class AuthProvider(str, enum.Enum):
    # 'password' is an auth-provider name, not a credential.
    password = "password"  # noqa: S105  # nosec B105
    google = "google"
    microsoft = "microsoft"


class StaffRole(str, enum.Enum):
    teacher = "teacher"
    support = "support"
    leadership = "leadership"
    district = "district"


class AdminRole(str, enum.Enum):
    """Internal-team roles for the admin console (FR-19-01). Distinct from school StaffRole —
    an InternalAdmin is a platform-level account, never school-scoped. Completes the 6-role
    matrix (4 staff + 2 internal): a full-control superadmin and a limited support role whose
    permission set is a strict subset (the deny-cell the RBAC probe exercises)."""

    superadmin = "superadmin"  # full platform control — every admin permission
    support = "support"        # limited internal support — subset only, no platform maintenance


class StaffStatus(str, enum.Enum):
    invited = "invited"
    sent = "sent"
    accepted = "accepted"
    active = "active"
    deactivated = "deactivated"


class StudentAgeBand(str, enum.Enum):
    b5_7 = "b5_7"
    b8_11 = "b8_11"
    b12_18 = "b12_18"


class StudentStatus(str, enum.Enum):
    active = "active"
    inactive = "inactive"


class SessionKind(str, enum.Enum):
    """Which surface a session belongs to — a student session can never reach a staff route."""
    staff = "staff"
    student = "student"
    admin = "admin"


# ---- remaining §13.4 / attribute enums (INFRA-02) ----
class StaffClassScope(str, enum.Enum):
    owner = "owner"
    shared = "shared"


class InvitationStatus(str, enum.Enum):
    invited = "invited"
    sent = "sent"
    accepted = "accepted"
    revoked = "revoked"
    expired = "expired"


class ActivityScope(str, enum.Enum):
    seed = "seed"
    school = "school"


class ActivityType(str, enum.Enum):
    breathing = "breathing"
    grounding = "grounding"
    stretch = "stretch"
    brain_break = "brain_break"


class ActivityAgeBand(str, enum.Enum):
    b5_7 = "b5_7"
    b8_11 = "b8_11"
    b12_18 = "b12_18"
    all = "all"


class ActivityEngagementStatus(str, enum.Enum):
    offered = "offered"
    started = "started"
    completed = "completed"


class FlagType(str, enum.Enum):
    concern_word = "concern_word"
    slow_burn = "slow_burn"


class FlagBand(str, enum.Enum):
    immediate = "immediate"
    triage = "triage"
    none = "none"


class FlagStatus(str, enum.Enum):
    open = "open"
    acknowledged = "acknowledged"
    escalated = "escalated"
    actioned = "actioned"
    closed = "closed"


class AlertChannel(str, enum.Enum):
    email = "email"
    in_app = "in_app"


class DeliveryStatus(str, enum.Enum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    retrying = "retrying"


class FlagEventType(str, enum.Enum):
    alerted = "alerted"
    viewed = "viewed"
    acted = "acted"
    escalated = "escalated"


class SubscriptionTier(str, enum.Enum):
    free = "free"
    premium = "premium"


class SubscriptionState(str, enum.Enum):
    trial = "trial"
    active = "active"
    free = "free"
    cancelled = "cancelled"


class ParentalConsentStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"


class DataExportKind(str, enum.Enum):
    export = "export"
    export_and_delete = "export_and_delete"


class DataExportStatus(str, enum.Enum):
    pending = "pending"
    ready = "ready"
    completed = "completed"
