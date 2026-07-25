"""Shared-class colleague invitation schemas (FR-02-03).

``extra="forbid"`` on every write body: no field beyond the ones named here can ever reach the
service layer (mirrors ``RegisterSchool`` / ``SchoolDecisionRequest`` — the codebase-wide
convention for closing off privilege-escalation-by-extra-field).
"""
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class SendInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class SendInvitationResponse(BaseModel):
    invitation_id: uuid.UUID
    status: Literal["sent"] = "sent"


class InvitationAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["resend", "revoke"]


class InvitationActionResponse(BaseModel):
    status: Literal["sent", "revoked"]


class AcceptInvitation(BaseModel):
    """``password`` is required only when the invitee has no existing account at the school yet
    (checked server-side — the client cannot know this in advance without an email-existence
    oracle, so the field is optional here and validated in the service layer)."""

    model_config = ConfigDict(extra="forbid")

    token: str
    password: str | None = Field(default=None, min_length=8, max_length=256)


class AcceptInvitationResponse(BaseModel):
    school_id: uuid.UUID
    class_id: uuid.UUID


class InvitationRow(BaseModel):
    id: uuid.UUID
    email: str
    status: Literal["invited", "sent", "accepted", "revoked", "expired"]


class ClassInvitationsResponse(BaseModel):
    """GET /classes/{id}/invitations — the 'Pending invitations' table (SC-059). Not in the
    ticket's literal DoD endpoint list; added so Resend/Revoke have real rows to act on instead of
    a fixture (same class of justified, logged addition as the other minimal GETs this ticket
    added — see ``src.routers.classes`` / ``preview_invitation``)."""

    invitations: list[InvitationRow]


class InvitationPreviewResponse(BaseModel):
    """GET /invitations/{token} — what AcceptInvite.tsx (SC-019) renders BEFORE the invitee
    commits: the class name + who invited them. Not in the ticket's DoD endpoint list, but the FE
    screen names real class/inviter data (never fabricated placeholder copy) — same class of
    justified, logged addition as FR-02-02's two read endpoints. ``inviter_email`` (not a name):
    ``StaffAccount`` has no display-name field anywhere in the model, the same real gap FR-04-01's
    review already found and resolved by dropping fabricated copy rather than inventing a field."""

    class_name: str
    inviter_email: str


class ErrorResponse(BaseModel):
    detail: str
