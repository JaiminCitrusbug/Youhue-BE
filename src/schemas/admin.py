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
