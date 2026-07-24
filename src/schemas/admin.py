"""Admin console request/response schemas (Pydantic v2).

Own module (decision #4) — the admin sign-in surface is disjoint from the staff/student surfaces.
"""
import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from src.constants.enums import SchoolStatus, SchoolTier, SubscriptionState


class AdminSignIn(BaseModel):
    """Admin console sign-in. `mfa_code` absent = request the email-OTP challenge
    (AdminMfaChallenge step); `mfa_code` present = complete sign-in."""

    email: EmailStr
    password: str = Field(max_length=256)
    mfa_code: str | None = None
    device_id: str | None = None


class AdminSignInResponse(BaseModel):
    """`mfa_required=True` + no session = the AdminMfaChallenge step (OTP emailed).
    On completion: `admin_session` bearer token + the caller's internal-team `role`."""

    admin_session: str | None = None
    role: str | None = None
    mfa_required: bool = False


class DefaultWordListUpdate(BaseModel):
    """FR-19-05 — the full replacement set for the platform default concern-word list (add/edit/
    remove entries by sending the new complete list). Server-side normalized + validated."""

    words: list[str] = Field(max_length=1000)


class DefaultWordListResponse(BaseModel):
    """The persisted platform default — normalized words + entry count. Returned by both the GET
    (current list, `words: []` until the internal team seeds one) and the PUT (the list as saved).
    `is_default` marks this as the platform default (a school override is a separate list)."""

    words: list[str]
    count: int
    is_default: bool = True


# ---- FR-19-02 — school accounts / trial extension / support access -----------------------------


class SchoolListItem(BaseModel):
    """One row of the SC-075 school-accounts table."""

    id: uuid.UUID
    name: str
    tier: SchoolTier
    status: SchoolStatus


class SchoolsListResponse(BaseModel):
    schools: list[SchoolListItem]
    count: int


class SchoolDetail(BaseModel):
    """SC-076/077 detail — the school's account fields + its trial state, if a Subscription row
    exists yet (`subscription_state`/`trial_end_at` are null until the school's first admin-console
    trial touch lazily creates one; see `src/domain/billing/services.py`)."""

    id: uuid.UUID
    name: str
    tier: SchoolTier
    status: SchoolStatus
    timezone: str
    subscription_state: SubscriptionState | None = None
    trial_end_at: datetime | None = None
    trial_extension_count: int = 0


class AdminSchoolAction(str, enum.Enum):
    """The three PATCH /admin/schools/{id} actions (FR-19-02)."""

    extend_trial = "extend_trial"
    update_account = "update_account"
    support_access = "support_access"


class SchoolActionRequest(BaseModel):
    """One request shape for all three actions — only the fields the chosen `action` uses are
    read; the others are ignored. `extend_trial` uses none. `update_account` uses `tier` /
    `status` / `timezone` (any subset — unset fields are left unchanged). `support_access` uses
    `reason` (required, non-blank; matches the approved SC-077 "Reason for access" field)."""

    action: AdminSchoolAction
    tier: SchoolTier | None = None
    status: SchoolStatus | None = None
    timezone: str | None = None
    reason: str | None = Field(default=None, max_length=2000)


class SchoolActionResponse(BaseModel):
    action: AdminSchoolAction
    school: SchoolDetail
