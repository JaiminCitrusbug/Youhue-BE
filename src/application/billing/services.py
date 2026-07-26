"""FR-17-03 — the whole-school Premium trial CLOCK. FR-02-02 already ARMS the trial at approval
(`domain.billing.services.arm_trial_subscription`: `tier=premium`, `state=trial`,
`trial_start_at=NULL`) — this module owns exactly one thing: setting `trial_start_at`/
`trial_end_at` from the school's first student check-in, never from approval, never twice.

The entitlement engine (FR-17-01) is the SEPARATE owner of what "Premium" grants — this module
never touches `tier`/`state`, only the window.

Server-side, idempotent [Baseline BR-05]: `get_or_create_subscription`'s `SELECT ... FOR UPDATE`
(domain layer) serializes two students at the same school checking in for the very first time
concurrently — the loser observes the winner's already-committed `trial_start_at` and no-ops,
never re-arming the clock (GATE G-10: the trial does not start before the first check-in, and
does not restart after it either).
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.domain.billing import services as billing_db
from src.domain.billing.models import Subscription
from src.domain.identity.models import StaffAccount

logger = logging.getLogger("youhue.billing")

_TRIAL_LENGTH = timedelta(weeks=6)
_ALREADY_STARTED = HTTPException(
    status.HTTP_409_CONFLICT, "The trial has already started for this school"
)
_FORBIDDEN = HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")


def start_trial_on_first_checkin(db: Session, school_id: uuid.UUID) -> Subscription | None:
    """Called from the check-in submit path (system-triggered, not user-initiated). Returns the
    updated `Subscription` if THIS call armed the clock, or `None` if it was already armed (a
    silent no-op here — this is the expected, common case on every check-in after the first, not
    an error; the explicit 409 is reserved for the deliberate HTTP endpoint below)."""
    sub = billing_db.get_or_create_subscription(db, school_id)
    if sub.trial_start_at is not None:
        return None
    now = datetime.now(UTC)
    sub.trial_start_at = now
    sub.trial_end_at = now + _TRIAL_LENGTH
    db.flush()
    logger.info(
        "fr_17_03_success action=start_trial school_id=%s trial_start_at=%s trial_end_at=%s",
        school_id, sub.trial_start_at.isoformat(), sub.trial_end_at.isoformat(),
    )
    return sub


def start_trial_via_endpoint(
    db: Session, staff: StaffAccount, school_id: uuid.UUID
) -> tuple[datetime, datetime]:
    """`POST /api/v1/schools/{id}/trial/start` (ticket DoD) — same underlying idempotent action as
    the check-in hook above, but a genuine re-attempt on an already-armed trial is a hard 409 here
    (never a re-armed clock), matching the ticket's own Scenario/DoD text for this endpoint.
    Staff-only, same-school-only (this ticket has no distinct role requirement of its own — mirrors
    the same-school posture every other staff-facing school action in this router already uses)."""
    if staff.school_id != school_id:
        logger.warning(
            "fr_17_03_forbidden action=start_trial actor_id=%s reason=cross_tenant "
            "school_id=%s", staff.id, school_id,
        )
        raise _FORBIDDEN
    sub = billing_db.get_or_create_subscription(db, school_id)
    if sub.trial_start_at is not None:
        logger.info(
            "fr_17_03_rejected action=start_trial school_id=%s reason=already_started", school_id
        )
        raise _ALREADY_STARTED
    now = datetime.now(UTC)
    end = now + _TRIAL_LENGTH
    sub.trial_start_at = now
    sub.trial_end_at = end
    db.flush()
    logger.info(
        "fr_17_03_success action=start_trial school_id=%s trial_start_at=%s trial_end_at=%s",
        school_id, now.isoformat(), end.isoformat(),
    )
    return now, end
