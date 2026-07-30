"""FR-17-04 — automatic downgrade to Free at trial end / non-renewal / non-payment (GATE G-11),
and FR-17-07 (folded in per the ticket's traceability line) — mid-term cancellation issues no
refund and keeps access to the paid term's actual end date (`term_end_at`).

FR-17-01 (`application/entitlements/services.py`) is the SEPARATE owner of what each tier grants;
FR-17-03 (`services.py` in this same package) is the SEPARATE owner of the trial *window* and
explicitly never touches `tier`/`state`. This module owns exactly the `Subscription.state` edges
into/through Free the domain model documents (`docs/design/domain-model.md` §13.4):
    trial     -> free        (trial end / non-payment while still trialling)
    active    -> free        (non-renewal: the paid term itself lapsed, no cancellation on record)
    active    -> cancelled   (mid-term cancellation — no refund, access continues to term_end_at)
    cancelled -> free        (term end reached after an earlier cancellation)
`trial -> active` (converting to paid) and `free -> active` (upgrade) are NOT this ticket's edges —
FR-17-06 is only the pricing/quote *presentation* surface, and a repo-wide grep confirms there is
still no in-platform "become paid" action anywhere in this codebase (external, quote-based billing,
per FR-17-06's own scope). This module only ever CONSUMES an already-`active` subscription; it
never creates one.

Server-side, ACID, idempotent [Baseline BR-05]: every write locks its Subscription row first
(`domain.billing.services.get_subscription_locked`) so a retry — or a concurrent second caller
racing the SAME row (the scheduled sweep and a manual admin retrigger, or two sweep passes) — never
double-applies a transition; the loser always observes the already-applied state and gets the same
409 a genuinely ineligible subscription would (never a lost update, never a duplicate transition).

'Premium data retained read-only behind a buy-Premium-to-unlock prompt' (GATE G-11, Scenario 2):
every transition here changes ONLY `tier`/`state` — `trial_start_at` / `trial_end_at` /
`trial_extension_count` / `term_end_at` are never cleared, and the Subscription row itself is never
deleted, so a school's subscription history is retained, never destroyed. This Phase-1 codebase has
no built Premium-feature-owned data tables yet (FR-17-01's own scope note: custom check-ins / AI
analysis / roster sync / calendar / school-district views / reports are explicitly deferred to
Phase 2/3, and a repo-wide grep of the FE confirms no screen anywhere currently gates its data
display on entitlements/tier at all). So the concrete, testable 'retained read-only behind an
upgrade prompt' surface THIS ticket owns is: (a) nothing is ever deleted by a downgrade or
cancellation, and (b) `GET /entitlements` already withdraws only the Premium feature *list*
(FR-17-01's `features_for_tier`, unchanged here) while the Subscription screen (FE) renders the
Free state with the upgrade prompt. Retrofitting per-screen premium-data gating across every other
already-merged screen is a cross-cutting change with no design reference in this ticket (the ticket
names exactly one: `approved/screens/Subscription.tsx`) and is out of scope here — logged, not
silently expanded.
"""
import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import SubscriptionState, SubscriptionTier
from src.domain.billing import services as billing_db
from src.domain.billing.models import Subscription

logger = logging.getLogger("youhue.billing")

_DOWNGRADE_CONFLICT = HTTPException(
    status.HTTP_409_CONFLICT, "Subscription is not eligible for downgrade"
)
_CANCEL_CONFLICT = HTTPException(
    status.HTTP_409_CONFLICT, "Subscription is not eligible for mid-term cancellation"
)


def _is_downgrade_eligible(sub: Subscription, now: datetime) -> bool:
    """The three routes INTO Free this ticket owns: trial end, non-renewal, term end after a
    cancellation. Anything else (already Free, or a real deadline that has not yet passed) is not
    eligible — never a silently-applied early downgrade."""
    if sub.state == SubscriptionState.trial:
        return sub.trial_end_at is not None and sub.trial_end_at <= now
    if sub.state in (SubscriptionState.active, SubscriptionState.cancelled):
        return sub.term_end_at is not None and sub.term_end_at <= now
    return False


def downgrade_subscription(db: Session, subscription_id: uuid.UUID) -> Subscription:
    """FR-17-04 / GATE G-11 — drop a school to Free at trial end, non-renewal, or (after an earlier
    mid-term cancellation) at the cancelled term's actual end. 200 `{state: 'free'}` on success;
    409 on an already-Free or not-yet-eligible subscription — idempotent, a retry never re-applies
    or errors past the first successful transition. A 500 (unexpected persistence failure) is the
    router's job to surface, never silently dropped — this function only raises the deliberate 409
    or lets a real exception propagate."""
    sub = billing_db.get_subscription_locked(db, subscription_id)
    if sub is None or not _is_downgrade_eligible(sub, datetime.now(UTC)):
        logger.info(
            "fr_17_04_rejected action=downgrade subscription_id=%s "
            "reason=ineligible_or_already_free",
            subscription_id,
        )
        raise _DOWNGRADE_CONFLICT
    sub.tier = SubscriptionTier.free
    sub.state = SubscriptionState.free
    db.flush()
    logger.info(
        "fr_17_04_success action=downgrade subscription_id=%s school_id=%s",
        subscription_id, sub.school_id,
    )
    return sub


def cancel_subscription(db: Session, subscription_id: uuid.UUID) -> Subscription:
    """FR-17-04/FR-17-07 — mid-term cancellation of a paid (`active`) plan: no refund is issued
    [there is no payment/refund logic in this platform to run — external, quote-based billing, per
    FR-17-06], and access continues to the paid term's actual end date: `term_end_at` is left
    completely unchanged and `tier` stays `premium`. Only `state` moves to `cancelled`; the school
    only actually drops to Free once `downgrade_subscription` is later called on this SAME row
    after `term_end_at` has passed (the scheduled sweep, or a repeat manual call). An `active`
    subscription with no `term_end_at` on record cannot honour 'access continues to the actual end
    date' (there is no end date to honour) — refused as ineligible (409) rather than guess one."""
    sub = billing_db.get_subscription_locked(db, subscription_id)
    if sub is None or sub.state != SubscriptionState.active or sub.term_end_at is None:
        logger.info(
            "fr_17_04_rejected action=cancel subscription_id=%s reason=ineligible", subscription_id
        )
        raise _CANCEL_CONFLICT
    sub.state = SubscriptionState.cancelled
    db.flush()
    logger.info(
        "fr_17_04_success action=cancel subscription_id=%s school_id=%s term_end_at=%s",
        subscription_id, sub.school_id, sub.term_end_at.isoformat(),
    )
    return sub


def run_downgrade_sweep(db: Session) -> list[uuid.UUID]:
    """The 'scheduled/background job that evaluates trial end / payment state' the ticket's Build
    order names — see `worker.py` for the process entrypoint. One pass over every non-Free
    subscription: whichever are due (trial past `trial_end_at`, active/cancelled past
    `term_end_at`) are downgraded, each re-locked and re-checked individually inside
    `downgrade_subscription` (the initial scan below is an unlocked snapshot — safe, because the
    real eligibility decision is re-validated under the row lock at transition time, never taken
    from the stale scan). Returns the school_ids actually downgraded this pass — an empty list
    (nothing due) is the normal, expected result on most passes, never an error."""
    now = datetime.now(UTC)
    candidates = db.scalars(
        select(Subscription).where(Subscription.state != SubscriptionState.free)
    ).all()
    due_ids = [row.id for row in candidates if _is_downgrade_eligible(row, now)]
    downgraded: list[uuid.UUID] = []
    for sub_id in due_ids:
        try:
            sub = downgrade_subscription(db, sub_id)
        except HTTPException:
            continue  # no longer eligible by the time we reached it (already handled) — skip
        downgraded.append(sub.school_id)
    return downgraded
