"""Admin console request/response schemas (Pydantic v2).

Own module (decision #4) — the admin sign-in surface is disjoint from the staff/student surfaces.
"""
from pydantic import BaseModel, EmailStr, Field


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
