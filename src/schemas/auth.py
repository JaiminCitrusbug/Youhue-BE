"""Auth request/response schemas (Pydantic v2)."""
import uuid

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


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  (OAuth token type label, not a secret)
    mfa_required: bool = False


class MeResponse(BaseModel):
    subject_id: uuid.UUID
    kind: str
    school_id: uuid.UUID | None = None
    role: str | None = None
