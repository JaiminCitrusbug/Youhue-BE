"""Shared-class colleague invitations (FR-02-03).

Invite/resend/revoke are class-owner-only, same-school (``StaffDep`` + the owner check in the
service layer, 403 otherwise). Accept is public/unauthenticated — the invitee has no account yet,
or is proving control of the invite link, never a session; it shares the ``auth`` rate-limit
bucket with sign-in/OTP-verify (a token-guessing surface, same threat shape). Thin router — all
business logic lives in ``src.application.invitations.services``.
"""
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status

from src.application.invitations import services as invitations_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.invitations import (
    AcceptInvitation,
    AcceptInvitationResponse,
    ClassInvitationsResponse,
    ErrorResponse,
    InvitationAction,
    InvitationActionResponse,
    InvitationPreviewResponse,
    SendInvitation,
    SendInvitationResponse,
)

logger = logging.getLogger("youhue.invitations")
router = APIRouter(tags=["invitations"])


@router.get(
    "/classes/{class_id}/invitations",
    response_model=ClassInvitationsResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Caller is not the owner of this class, or belongs to a different "
            "school.",
        },
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such class."},
    },
)
def list_class_invitations(
    class_id: uuid.UUID, staff: StaffDep, db: DbDep
) -> ClassInvitationsResponse:
    """SC-059 — the 'Pending invitations' table. Read-only (no transaction to commit/roll back)."""
    return invitations_svc.list_class_invitations(db, staff, class_id)


@router.post(
    "/classes/{class_id}/invitations",
    response_model=SendInvitationResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Caller is not the owner of this class, or belongs to a different "
            "school.",
        },
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such class."},
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "This email already has a pending invitation to this class.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Invitation failed; nothing was written.",
        },
    },
)
def invite_colleague(
    class_id: uuid.UUID, body: SendInvitation, staff: StaffDep, db: DbDep
) -> SendInvitationResponse:
    """SC-059 — invite a colleague to co-teach this class by email (single-use expiring token)."""
    try:
        result = invitations_svc.invite_colleague(db, staff, class_id, body.email)
    except HTTPException:
        # no partial invitation survives a forbidden/not-found/duplicate/send-failed call
        db.rollback()
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_02_03_error action=invite")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Invitation failed") from exc
    db.commit()
    return result


@router.post(
    "/invitations/{invitation_id}/action",
    response_model=InvitationActionResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "Caller is not the owner of this class, or belongs to a different "
            "school.",
        },
        status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No such invitation."},
        status.HTTP_409_CONFLICT: {
            "model": ErrorResponse,
            "description": "The invitation is no longer pending (already accepted/revoked/"
            "expired).",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Action failed; nothing was written.",
        },
    },
)
def action_on_invitation(
    invitation_id: uuid.UUID, body: InvitationAction, staff: StaffDep, db: DbDep
) -> InvitationActionResponse:
    """Re-send (fresh single-use token) or revoke a pending invitation. Class-owner-only."""
    try:
        result = invitations_svc.action_on_invitation(db, staff, invitation_id, body.action)
    except HTTPException:
        db.rollback()  # no partial resend/revoke survives a forbidden/not-found/conflict call
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_02_03_error action=%s", body.action)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Action failed") from exc
    db.commit()
    return result


@router.get(
    "/invitations/{token}",
    response_model=InvitationPreviewResponse,
    dependencies=[Depends(rate_limit)],
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponse,
            "description": "Invalid, expired, or already-used invitation token.",
        },
    },
)
def preview_invitation(token: str, db: DbDep) -> InvitationPreviewResponse:
    """SC-019 pre-accept view — public, unauthenticated, read-only (no transaction to commit)."""
    return invitations_svc.preview_invitation(db, token)


@router.post(
    "/invitations/accept",
    response_model=AcceptInvitationResponse,
    dependencies=[Depends(rate_limit)],
    responses={
        status.HTTP_400_BAD_REQUEST: {
            "model": ErrorResponse,
            "description": "Invalid, expired, or already-used invitation token.",
        },
        status.HTTP_403_FORBIDDEN: {
            "model": ErrorResponse,
            "description": "The invitee's existing account at this school is deactivated.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "A password is required (the invitee has no account here yet).",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Accept failed; nothing was written.",
        },
    },
)
def accept_invitation(body: AcceptInvitation, db: DbDep) -> AcceptInvitationResponse:
    """SC-019 — public, unauthenticated. Grants access to ONLY the shared class (GATE G-4), never
    the whole school."""
    try:
        result = invitations_svc.accept_invitation(db, body.token, body.password)
    except HTTPException:
        db.rollback()  # no partial acceptance survives an invalid/expired/deactivated/422 call
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_02_03_error action=accept")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Accept failed") from exc
    db.commit()
    return result
