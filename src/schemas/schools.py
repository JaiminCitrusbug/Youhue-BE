"""School self-registration (FR-02-01) + District/Trust approval (FR-02-02) schemas.

A teacher submits the new school's details plus their own account. Validation is server-side and
authoritative (client validation is convenience only, ticket §Interaction contract): ``school_name``
must be non-empty after trimming and ``registrant_email`` must be a valid RFC-5322 address.

The error models below exist so the endpoint's real status codes appear in ``/openapi.json`` — a
generated client that only knows 201/422 treats 409/429 as unexpected transport errors.
"""
import uuid
from datetime import datetime
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


# --- FR-02-02: District/Trust leadership reviews + decides a pending school -----------------------


class PendingSchoolOut(BaseModel):
    """One row of the District approval queue (SC-069)."""

    school_id: uuid.UUID
    name: str
    registrant_email: str | None
    created_at: datetime


class PendingSchoolsResponse(BaseModel):
    """GET /schools/pending — oldest-first (SC-069 hint 'oldest first')."""

    schools: list[PendingSchoolOut]


class SchoolDetailResponse(BaseModel):
    """GET /schools/{id} — the single-school review view (SC-070). ``student_count`` is a genuine
    count of existing ``Student`` rows for the school — expected 0 while a school is pending, since
    no student can exist there until roster import (FR-03-01, not built here)."""

    school_id: uuid.UUID
    name: str
    status: Literal["pending", "active", "rejected"]
    registrant_email: str | None
    student_count: int
    created_at: datetime


class SchoolDecisionRequest(BaseModel):
    """POST /schools/{id}/decision body. ``extra="forbid"`` for the same reason as
    ``RegisterSchool``: no field beyond the one decision can ever escalate the write."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]


class SchoolDecisionResponse(BaseModel):
    """200 body. ``status`` mirrors the ``SchoolStatus`` the decision produced — never ``pending``,
    since a decision always resolves the school one way or the other."""

    school_id: uuid.UUID
    status: Literal["active", "rejected"]


class DecisionConflictDetail(BaseModel):
    code: Literal["not_pending"]
    message: str


class DecisionConflictResponse(BaseModel):
    """409 body — the school exists but is no longer pending (already decided, or a retry after a
    first decision already committed). Idempotency-on-retry [BR-05]: a second decision call never
    re-arms a trial or re-flips a rejection; it is a terminal conflict."""

    detail: DecisionConflictDetail


class TrialStartResponse(BaseModel):
    """FR-17-03 — 200 body for `POST /schools/{id}/trial/start`."""

    trial_start_at: datetime
    trial_end_at: datetime
