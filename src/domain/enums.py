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
    password = "password"  # noqa: S105  (enum value, not a secret)
    google = "google"
    microsoft = "microsoft"


class StaffRole(str, enum.Enum):
    teacher = "teacher"
    support = "support"
    leadership = "leadership"
    district = "district"


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
