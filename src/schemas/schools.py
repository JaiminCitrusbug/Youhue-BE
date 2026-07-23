"""School self-registration schemas (FR-02-01).

A teacher submits the new school's details plus their own account. Validation is server-side and
authoritative (client validation is convenience only, ticket §Interaction contract): ``school_name``
must be non-empty after trimming and ``registrant_email`` must be a valid RFC-5322 address.

The error models below exist so the endpoint's real status codes appear in ``/openapi.json`` — a
generated client that only knows 201/422 treats 409/429 as unexpected transport errors.
"""
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints

# Non-empty after trimming surrounding whitespace ("   " is rejected, not stored as a blank name).
SchoolName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class RegisterSchool(BaseModel):
    """Teacher-supplied registration payload: the school details + the registrant's account.

    ``extra="forbid"``: an unknown field is a 422, never silently dropped. There is no ``role`` /
    ``status`` / ``school_id`` field and there must not be one — forbidding extras makes "the body
    cannot escalate privilege" an enforced property rather than an incidental one."""

    model_config = ConfigDict(extra="forbid")

    school_name: SchoolName
    registrant_email: EmailStr
    password: str = Field(min_length=8, max_length=256)


class RegisterSchoolResponse(BaseModel):
    """201 body — the pending school this registrant now owns.

    ``status`` is the literal ``"pending"``: this endpoint never yields a live school (approval is
    FR-02-02), so a regression that returned ``"active"`` is a type error, not a silent contract
    break."""

    school_id: uuid.UUID
    status: Literal["pending"] = "pending"


class ConflictDetail(BaseModel):
    """Machine-readable reason for the 409. ``code`` is the field to branch on; ``message`` is
    display copy and may change without notice."""

    code: Literal["school_exists"]
    message: str


class ConflictResponse(BaseModel):
    """409 body. Carries NO ``school_id`` on purpose: the caller is unauthenticated, there is no
    join/membership endpoint that could consume one, and handing a tenant UUID (plus a confirmation
    that the tenant is real) to an anonymous caller is gratuitous disclosure."""

    detail: ConflictDetail


class ErrorResponse(BaseModel):
    """429 / 500 body — FastAPI's plain-string ``detail``."""

    detail: str
