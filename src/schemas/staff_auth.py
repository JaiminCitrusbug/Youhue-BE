"""Staff auth request schemas (email/password sign-in, email-OTP MFA, single-use reset).

Split out of the shared ``src.schemas.auth`` grouping (decision #4) so the staff auth surface is
independently ownable. Shared response shapes (``TokenResponse``) stay in ``src.schemas.auth``.
"""
from pydantic import BaseModel, EmailStr, Field


class StaffSignIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)
    device_id: str | None = None


class ForgotPassword(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=256)


class MfaVerify(BaseModel):
    session_token: str
    code: str
