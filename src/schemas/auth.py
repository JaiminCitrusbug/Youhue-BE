"""Shared auth schemas: the session token response and the /me probe.

Student request schemas live in ``src.schemas.student_auth`` and staff-specific request schemas in
``src.schemas.staff_auth`` (decision #4 module split); ``TokenResponse`` is shared by every sign-in
surface and stays here.
"""
import uuid

from pydantic import BaseModel


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105  (OAuth token type label, not a secret)
    mfa_required: bool = False


class MeResponse(BaseModel):
    subject_id: uuid.UUID
    kind: str
    school_id: uuid.UUID | None = None
    role: str | None = None
