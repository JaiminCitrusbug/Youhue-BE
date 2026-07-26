"""FR-02-04: the single state machine every staff-account status mutation goes through.

Named lifecycle: invited -> sent -> accepted -> active -> deactivated (SRS verbatim). Server-side
validation is authoritative [Baseline BR-05] — an illegal hop (e.g. active -> invited, or anything
out of deactivated) is a 409, never silently accepted or silently reinterpreted. A same-state
request (e.g. PATCH deactivated on an already-deactivated account) is treated as an idempotent
no-op success [BR-05], matching this codebase's established retry-idempotency precedent
(``leadership.services.deactivate_staff``, which this module now backs).

``advance_to`` walks MULTIPLE legal hops in one call (e.g. invited -> ... -> active) for internal,
system-initiated callers (invitation acceptance) where every intermediate state is genuinely
passed through within one DB transaction, never skipped — it is not a shortcut around the graph,
it just calls ``transition`` once per hop.
"""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.isolation import services as isolation_svc
from src.constants.enums import StaffRole, StaffStatus
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount

logger = logging.getLogger("youhue.staff_lifecycle")

# The only legal forward hops. `deactivated` is terminal (no outbound edges) — once withdrawn, an
# account is never silently reactivated by this endpoint (a fresh invite/registration is a new
# decision, out of this state machine's scope).
_LEGAL_TRANSITIONS: dict[StaffStatus, frozenset[StaffStatus]] = {
    StaffStatus.invited: frozenset({StaffStatus.sent, StaffStatus.deactivated}),
    StaffStatus.sent: frozenset({StaffStatus.accepted, StaffStatus.deactivated}),
    StaffStatus.accepted: frozenset({StaffStatus.active, StaffStatus.deactivated}),
    StaffStatus.active: frozenset({StaffStatus.deactivated}),
    StaffStatus.deactivated: frozenset(),
}

_ILLEGAL_TRANSITION = HTTPException(status.HTTP_409_CONFLICT, "Illegal status transition")
_STAFF_NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, "Staff account not found")


def transition(db: Session, staff: StaffAccount, target: StaffStatus) -> StaffAccount:
    """One legal hop. Idempotent no-op if already at ``target``; 409 if the hop isn't in the
    graph above — never a silent reinterpretation of an illegal request."""
    if staff.status == target:
        return staff
    if target not in _LEGAL_TRANSITIONS.get(staff.status, frozenset()):
        logger.info(
            "fr_02_04_rejected reason=illegal_transition staff_id=%s from=%s to=%s",
            staff.id, staff.status.value, target.value,
        )
        raise _ILLEGAL_TRANSITION
    staff.status = target
    db.flush()
    return staff


def advance_to(db: Session, staff: StaffAccount, target: StaffStatus) -> StaffAccount:
    """Walk every legal intermediate hop from the account's CURRENT status to ``target`` — used by
    system-initiated flows (e.g. invitation acceptance) where the account genuinely passes through
    each named state within one transaction, never jumps straight there."""
    order = (
        StaffStatus.invited,
        StaffStatus.sent,
        StaffStatus.accepted,
        StaffStatus.active,
    )
    if target not in order:
        return transition(db, staff, target)  # e.g. deactivated — a single direct hop
    start = order.index(staff.status) if staff.status in order else -1
    end = order.index(target)
    if start >= end:
        return staff  # already there or ahead — no-op, not an error
    for step in order[start + 1 : end + 1]:
        transition(db, staff, step)
    return staff


def set_staff_status(
    db: Session, actor: StaffAccount, staff_id: uuid.UUID, new_status: StaffStatus
) -> StaffAccount:
    """``PATCH /api/v1/staff/{id}/status`` — the general lifecycle-accuracy endpoint (ticket DoD).
    Same authorisation posture as the parallel FR-16-02 deactivation surface it now shares a state
    machine with: leadership, same-school-only. Never re-implements FR-16-02's UI/endpoint — this
    is the one general status mutation surface; FR-16-02's own endpoint calls ``transition``
    directly (see ``leadership.services.deactivate_staff``)."""
    if actor.role != StaffRole.leadership:
        logger.warning(
            "fr_02_04_forbidden action=set_status actor_id=%s reason=role", actor.id
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    # School-scoped lookup: a cross-school staff_id resolves to None exactly like a genuinely
    # unknown one (mirrors `leadership.deactivate_staff`'s `get_staff_in_school` precedent) — the
    # response never distinguishes "doesn't exist" from "exists at another school". A REAL
    # cross-tenant reach (the id exists, just not here) is still audited via the unscoped lookup
    # below, same pattern as `invitations.services._audit_cross_tenant`.
    unscoped = identity_db.get_staff(db, staff_id)
    if unscoped is not None and unscoped.school_id != actor.school_id:
        isolation_svc.audit(
            db, actor_id=actor.id, action="staff.set_status.cross_tenant_denied",
            target=str(staff_id), school_id=actor.school_id,
        )
        db.commit()
    target = identity_db.get_staff_in_school_for_update(db, actor.school_id, staff_id)
    if target is None:
        logger.info(
            "fr_02_04_rejected action=set_status actor_id=%s reason=not_found staff_id=%s",
            actor.id, staff_id,
        )
        raise _STAFF_NOT_FOUND

    result = transition(db, target, new_status)
    logger.info(
        "fr_02_04_success action=set_status actor_id=%s staff_id=%s status=%s",
        actor.id, staff_id, result.status.value,
    )
    return result
