"""Shared-class colleague invitations (FR-02-03): invite / resend / revoke / accept.

A staff member who OWNS a class (``StaffClassAccess.scope=owner``) invites a colleague by email.
The invitation is a single-use, expiring token (INFRA-02's ``Invitation`` table, stored raw —
the same plaintext-token-at-rest precedent ``ClassGroup.join_code``/``qr_token`` already set for
this codebase, not the hashed-reset-token pattern, since ``Invitation.token`` has no separate hash
column to migrate). Accepting it grants access ONLY to the shared class
(``StaffClassAccess.scope=shared``) — never whole-school access (GATE G-4).

Colleague accounts created here get ``StaffRole.support`` — the ONLY role whose class-scope set is
shared-only (``application.authz.services._SUPPORT_SCOPES``), so "shared class only, never whole
school" is enforced by the SAME mechanism every other student-access check already relies on, not a
parallel rule invented for this ticket.

Re-send issues a FRESH token (overwriting the row's ``token`` column, after saving the old one to
``previous_token`` — FR-02-04) — the old token immediately stops resolving via
``get_invitation_by_token``, which is what makes a superseded invitation die. Revoke sets
``status=revoked``; accept checks status is still invited/sent before doing anything.

FR-02-04 (staff lifecycle accuracy): a StaffAccount created here is walked through
invited -> sent -> accepted -> active via ``staff_lifecycle.advance_to`` (never default-constructed
straight at ``active``), and a stale-token holder gets a message SPECIFIC to why the link is dead
(superseded by resend / revoked / expired), not one generic bucket (ticket Scenario 3).

Structured logs: ``fr_02_03_success`` / ``_rejected`` / ``_forbidden`` / ``_error``.
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.isolation import services as isolation_svc
from src.application.staff_lifecycle import services as staff_lifecycle
from src.constants.enums import InvitationStatus, StaffClassScope, StaffRole, StaffStatus
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount
from src.domain.org import services as org_db
from src.domain.org.models import Invitation
from src.infrastructure.emailer import send_email
from src.schemas.invitations import (
    AcceptInvitationResponse,
    ClassInvitationsResponse,
    InvitationActionResponse,
    InvitationPreviewResponse,
    InvitationRow,
    SendInvitationResponse,
)
from src.utils import security

logger = logging.getLogger("youhue.invitations")

_INVITATION_TTL = timedelta(days=7)

_NOT_CLASS_OWNER = HTTPException(status.HTTP_403_FORBIDDEN, "Only the class owner may invite")
_CLASS_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Class not found")
_INVITATION_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Invitation not found")
_ALREADY_ACTIONED = HTTPException(
    status.HTTP_409_CONFLICT, "This invitation is no longer pending"
)
_ALREADY_INVITED = HTTPException(status.HTTP_409_CONFLICT, "Already invited")
_INVALID_OR_EXPIRED = HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid or expired invitation")
# FR-02-04 Scenario 3: a superseded/revoked/expired link gets a SPECIFIC reason, never the one
# generic bucket above (still returned for a token that never existed at all — genuinely
# indistinguishable from "not found").
_SUPERSEDED = HTTPException(
    status.HTTP_400_BAD_REQUEST,
    "This invitation is no longer valid — a newer invitation has been sent. "
    "Please use the most recent invite email.",
)
_REVOKED = HTTPException(
    status.HTTP_400_BAD_REQUEST, "This invitation has been revoked and is no longer valid."
)
_EXPIRED = HTTPException(
    status.HTTP_400_BAD_REQUEST, "This invitation has expired and is no longer valid."
)
_PASSWORD_REQUIRED = HTTPException(
    status.HTTP_422_UNPROCESSABLE_ENTITY, "A password is required to create your account"
)
_ACCOUNT_DEACTIVATED = HTTPException(status.HTTP_403_FORBIDDEN, "Account is deactivated")


def _specific_invalid_reason(invitation: Invitation, presented_token: str) -> HTTPException:
    """FR-02-04 Scenario 3: turn a resolved-but-unusable invitation row into the SPECIFIC reason
    it's dead, instead of the one generic bucket. Only reached once ``get_invitation_by_any_token``
    has already found a real row — a token that matches nothing at all still gets the generic
    ``_INVALID_OR_EXPIRED``, which is honest (there's no "reason" to report for pure garbage)."""
    if invitation.token != presented_token:
        return _SUPERSEDED  # matched via previous_token — a resend superseded this exact link
    if invitation.status == InvitationStatus.revoked:
        return _REVOKED
    if invitation.status == InvitationStatus.expired or invitation.expires_at <= datetime.now(UTC):
        return _EXPIRED
    return _INVALID_OR_EXPIRED


def _require_class_owner(db: Session, staff: StaffAccount, class_id: uuid.UUID) -> None:
    access = org_db.get_staff_class_access(db, staff.id, class_id)
    if access is None or access.scope != StaffClassScope.owner:
        raise _NOT_CLASS_OWNER


def _audit_cross_tenant(db: Session, staff: StaffAccount, action: str, target: str) -> None:
    """FR-20-07: immutable audit for a REAL cross-tenant reach attempt (the resource exists, just
    not at the caller's school) — committed immediately, same as `students/services.py::_audit`,
    since the router rolls back the session when it catches the HTTPException raised right after
    this call; an audit row that only lived in the same doomed transaction would vanish with it."""
    isolation_svc.audit(
        db, actor_id=staff.id, action=action, target=target, school_id=staff.school_id
    )
    db.commit()


def _dispatch(invitation: Invitation, class_name: str) -> None:
    """Send the invitation email. A send failure propagates (never caught here) so the invitation
    row it belongs to is rolled back by the router — 'sent' is never claimed unless dispatch
    actually succeeded [INFRA-05: delivery confirmed, never silently lost]."""
    link = f"{settings.frontend_base_url}/accept-invite?token={invitation.token}"
    send_email(
        invitation.email,
        "You've been invited to a Youhue class",
        f"You have been invited to co-teach {class_name} on Youhue. "
        f"Accept your invitation: {link} (token={invitation.token})",
    )


def list_class_invitations(
    db: Session, staff: StaffAccount, class_id: uuid.UUID
) -> ClassInvitationsResponse:
    """GET-side, read-only: the 'Pending invitations' table (SC-059). Class-owner-only, same
    school — no side effect, no transaction to commit/roll back."""
    klass = org_db.get_class(db, class_id)
    if klass is not None and klass.school_id != staff.school_id:
        _audit_cross_tenant(db, staff, "invitation.list.cross_tenant_denied", str(class_id))
    if klass is None or klass.school_id != staff.school_id:
        logger.info("fr_02_03_rejected reason=class_not_found class_id=%s", class_id)
        raise _CLASS_NOT_FOUND
    try:
        _require_class_owner(db, staff, class_id)
    except HTTPException:
        logger.warning(
            "fr_02_03_forbidden reason=not_class_owner actor_id=%s class_id=%s", staff.id, class_id
        )
        raise
    rows = org_db.list_invitations_for_class(db, class_id)
    return ClassInvitationsResponse(
        invitations=[
            InvitationRow(id=r.id, email=r.email, status=r.status.value) for r in rows
        ]
    )


def invite_colleague(
    db: Session, staff: StaffAccount, class_id: uuid.UUID, email: str
) -> SendInvitationResponse:
    klass = org_db.get_class(db, class_id)
    if klass is not None and klass.school_id != staff.school_id:
        _audit_cross_tenant(db, staff, "invitation.invite.cross_tenant_denied", str(class_id))
    if klass is None or klass.school_id != staff.school_id:
        logger.info("fr_02_03_rejected reason=class_not_found class_id=%s", class_id)
        raise _CLASS_NOT_FOUND
    try:
        _require_class_owner(db, staff, class_id)
    except HTTPException:
        logger.warning(
            "fr_02_03_forbidden reason=not_class_owner actor_id=%s class_id=%s", staff.id, class_id
        )
        raise

    email_norm = email.lower()
    if org_db.get_pending_invitation_for_class(db, class_id, email_norm) is not None:
        logger.info(
            "fr_02_03_rejected reason=already_invited class_id=%s email=%s", class_id, email_norm
        )
        raise _ALREADY_INVITED

    invitation = org_db.create_invitation(
        db,
        school_id=staff.school_id,
        class_id=class_id,
        inviter_id=staff.id,
        email=email_norm,
        token=security.new_url_token(),
        expires_at=datetime.now(UTC) + _INVITATION_TTL,
    )
    _dispatch(invitation, klass.name)
    invitation.status = InvitationStatus.sent
    db.flush()
    logger.info(
        "fr_02_03_success event=invited invitation_id=%s class_id=%s actor_id=%s",
        invitation.id, class_id, staff.id,
    )
    return SendInvitationResponse(invitation_id=invitation.id, status="sent")


def action_on_invitation(
    db: Session, staff: StaffAccount, invitation_id: uuid.UUID, action: str
) -> InvitationActionResponse:
    """``action``: ``resend`` (fresh single-use token, re-dispatched) or ``revoke`` (kills the
    token outright). Class-owner-only, same-school-only; 409 if the invitation is not currently
    pending (already accepted/revoked/expired — no re-actioning a resolved invitation)."""
    invitation = org_db.get_invitation(db, invitation_id)
    if invitation is not None and invitation.school_id != staff.school_id:
        _audit_cross_tenant(
            db, staff, f"invitation.{action}.cross_tenant_denied", str(invitation_id)
        )
    if invitation is None or invitation.school_id != staff.school_id or invitation.class_id is None:
        logger.info("fr_02_03_rejected reason=not_found invitation_id=%s", invitation_id)
        raise _INVITATION_NOT_FOUND
    class_id: uuid.UUID = invitation.class_id
    try:
        _require_class_owner(db, staff, class_id)
    except HTTPException:
        logger.warning(
            "fr_02_03_forbidden reason=not_class_owner actor_id=%s invitation_id=%s",
            staff.id, invitation_id,
        )
        raise

    if invitation.status not in (InvitationStatus.invited, InvitationStatus.sent):
        logger.info(
            "fr_02_03_rejected reason=not_pending invitation_id=%s status=%s",
            invitation_id, invitation.status.value,
        )
        raise _ALREADY_ACTIONED

    if action == "revoke":
        invitation.status = InvitationStatus.revoked
        db.flush()
        logger.info(
            "fr_02_03_success event=revoked invitation_id=%s actor_id=%s", invitation_id, staff.id
        )
        return InvitationActionResponse(status="revoked")

    klass = org_db.get_class(db, class_id)
    class_name = klass.name if klass is not None else ""
    invitation.previous_token = invitation.token  # FR-02-04: recognise the old link specifically
    invitation.token = security.new_url_token()  # fresh token — the old one stops resolving
    invitation.expires_at = datetime.now(UTC) + _INVITATION_TTL
    _dispatch(invitation, class_name)
    invitation.status = InvitationStatus.sent
    db.flush()
    logger.info(
        "fr_02_03_success event=resent invitation_id=%s actor_id=%s", invitation_id, staff.id
    )
    return InvitationActionResponse(status="sent")


def preview_invitation(db: Session, token: str) -> InvitationPreviewResponse:
    """GET-side, read-only: what SC-019 shows BEFORE the invitee commits. No write, no side
    effect — an expired invitation here is just reported invalid; it is marked ``expired`` only
    on an actual accept attempt (``accept_invitation`` below)."""
    invitation = org_db.get_invitation_by_any_token(db, token)
    if invitation is None or invitation.class_id is None:
        raise _INVALID_OR_EXPIRED
    if (
        invitation.status not in (InvitationStatus.invited, InvitationStatus.sent)
        or invitation.expires_at <= datetime.now(UTC)
        or invitation.token != token
    ):
        raise _specific_invalid_reason(invitation, token)
    klass = org_db.get_class(db, invitation.class_id)
    inviter = identity_db.get_staff(db, invitation.inviter_id)
    return InvitationPreviewResponse(
        class_name=klass.name if klass is not None else "",
        inviter_email=inviter.email if inviter is not None else "",
    )


def accept_invitation(
    db: Session, token: str, password: str | None
) -> AcceptInvitationResponse:
    """Public/unauthenticated: the caller proves control of the invite by presenting its secret
    token, never a session. Creates the colleague's account (``StaffRole.support``, shared-scoped)
    if they have none at this school yet; otherwise reuses/activates their existing account and
    just grants the extra class access — no whole-school access is ever granted either way."""
    invitation = org_db.get_invitation_by_any_token(db, token)
    if invitation is None or invitation.class_id is None:
        logger.info("fr_02_03_rejected reason=invalid_or_used_token")
        raise _INVALID_OR_EXPIRED
    if invitation.status not in (InvitationStatus.invited, InvitationStatus.sent) or (
        invitation.token != token
    ):
        logger.info(
            "fr_02_03_rejected reason=invalid_or_used_token invitation_id=%s status=%s",
            invitation.id, invitation.status.value,
        )
        raise _specific_invalid_reason(invitation, token)
    class_id: uuid.UUID = invitation.class_id
    if invitation.expires_at <= datetime.now(UTC):
        invitation.status = InvitationStatus.expired
        db.flush()
        logger.info("fr_02_03_rejected reason=expired invitation_id=%s", invitation.id)
        raise _EXPIRED

    existing_staff = identity_db.get_staff_by_email_in_school(
        db, invitation.email, invitation.school_id
    )
    if existing_staff is not None:
        if existing_staff.status == StaffStatus.deactivated:
            logger.warning(
                "fr_02_03_forbidden reason=deactivated_account invitation_id=%s", invitation.id
            )
            raise _ACCOUNT_DEACTIVATED
        staff = existing_staff
        # FR-02-04: an already-invited account (e.g. self-registered but not yet approved, or
        # invited to a different class earlier) walks the real graph to `active`, never jumps.
        staff_lifecycle.advance_to(db, staff, StaffStatus.active)
    else:
        if not password:
            logger.info(
                "fr_02_03_rejected reason=password_required invitation_id=%s", invitation.id
            )
            raise _PASSWORD_REQUIRED
        # FR-02-04: created at the model's least-privilege `invited` default, then walked through
        # every named state in turn (invited -> sent -> accepted -> active) within this same
        # transaction — never default-constructed straight at `active`.
        staff = identity_db.create_staff(
            db,
            school_id=invitation.school_id,
            email=invitation.email,
            password_hash=security.hash_password(password),
            role=StaffRole.support,
        )
        staff_lifecycle.advance_to(db, staff, StaffStatus.active)

    org_db.grant_class_access(db, staff.id, class_id, scope=StaffClassScope.shared)
    invitation.status = InvitationStatus.accepted
    db.flush()
    logger.info(
        "fr_02_03_success event=accepted invitation_id=%s staff_id=%s class_id=%s",
        invitation.id, staff.id, class_id,
    )
    return AcceptInvitationResponse(school_id=invitation.school_id, class_id=class_id)
