"""Student auth request/response schemas (FR-01-02).

Split out of the shared `schemas.auth` grouping (decision #4) so the student surface owns its own
contract. Sign-in is passwordless: a `school_or_class_code` OR a class `qr_token` (mutually
exclusive), plus the chosen `student_id` (name/avatar selection).
"""
import uuid

from pydantic import BaseModel


class StudentSignIn(BaseModel):
    # Exactly one of these resolves the class/school; both-or-neither is rejected (400).
    school_or_class_code: str | None = None
    qr_token: str | None = None
    student_id: uuid.UUID
    device_id: str | None = None


class StudentSession(BaseModel):
    """FR-01-02 success shape: `200 { session_token, student_id, age_band }`."""

    session_token: str
    student_id: uuid.UUID
    age_band: str
