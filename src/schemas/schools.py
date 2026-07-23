"""School self-registration schemas (FR-02-01).

A teacher submits the new school's details plus their own account. Validation is server-side and
authoritative (client validation is convenience only, ticket §Interaction contract): ``school_name``
must be non-empty after trimming and ``registrant_email`` must be a valid RFC-5322 address.
"""
import uuid
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, StringConstraints

# Non-empty after trimming surrounding whitespace ("   " is rejected, not stored as a blank name).
SchoolName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RegisterSchool(BaseModel):
    """Teacher-supplied registration payload: the school details + the registrant's account."""

    school_name: SchoolName
    registrant_email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class RegisterSchoolResponse(BaseModel):
    """201 body — the created (or idempotently-replayed) school and its lifecycle status."""

    school_id: uuid.UUID
    status: str
