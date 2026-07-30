"""FR-17-04 (+ FR-17-07 folded in, per the ticket's own traceability line) — Subscription
lifecycle actions: automatic downgrade to Free (GATE G-11) and mid-term cancellation (no refund,
access continues to the paid term's actual end date).

  POST /api/v1/subscriptions/{id}/downgrade
        -> 200 {state: 'free', ...} | 403 role lacks manage_accounts | 409 ineligible/already-Free
        | 500 nothing written
  POST /api/v1/subscriptions/{id}/cancel
        -> 200 {state: 'cancelled', ...} | 403 role lacks manage_accounts | 409 ineligible
        | 500 nothing written

Both are internal-team/system-authoritative actions (ticket: "Authentication: System; it is not a
user delete") — admin-console-gated (`manage_accounts`, the SAME permission FR-19-02's other
billing-lifecycle admin actions — `extend_trial`/`update_account` — already use), reusing the
FR-19-01 admin RBAC policy layer as-is, never a parallel auth system. The actual trial-end /
non-renewal sweep that drives this in production has NO http hop at all —
`application/billing/worker.py` (`python -m src.application.billing.worker`) calls
`run_downgrade_sweep` directly against the DB. These endpoints are the deliberate manual/ops
trigger the ticket's own DoD literally names (`POST .../downgrade`), mirroring the FR-17-03
precedent of a system-triggered function PLUS a separate explicit endpoint.

Thin router — business logic lives in `src.application.billing.downgrade`.
"""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.authz import admin as admin_authz
from src.application.billing import downgrade as downgrade_svc
from src.domain.billing.models import Subscription
from src.infrastructure.middlewares.auth_middleware import DbDep
from src.routers.admin import AdminDep
from src.schemas.billing import SubscriptionActionResponse

router = APIRouter(prefix="/subscriptions", tags=["billing"])
logger = logging.getLogger("youhue.billing")


def _out(sub: Subscription) -> SubscriptionActionResponse:
    return SubscriptionActionResponse(
        id=sub.id, school_id=sub.school_id, tier=sub.tier, state=sub.state,
        term_end_at=sub.term_end_at,
    )


@router.post(
    "/{subscription_id}/downgrade",
    response_model=SubscriptionActionResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor's role lacks manage_accounts."},
        status.HTTP_409_CONFLICT: {"description": "Ineligible or already-Free subscription."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Downgrade failed; nothing was written."
        },
    },
)
def downgrade(
    subscription_id: uuid.UUID, admin: AdminDep, db: DbDep
) -> SubscriptionActionResponse:
    """GATE G-11 — drop the subscription to Free at trial end / non-renewal / term-end-after-
    cancellation. Only `tier`/`state` change: premium features are withdrawn (FR-17-01's
    `features_for_tier` already reads `tier`), the Free core keeps working, and no data is deleted
    (see `application/billing/downgrade.py` module docstring for what 'retained' covers here)."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_accounts)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the 403
        logger.warning(
            "fr_17_04_forbidden action=downgrade actor_id=%s subscription_id=%s",
            admin.id, subscription_id,
        )
        raise
    try:
        sub = downgrade_svc.downgrade_subscription(db, subscription_id)
    except HTTPException:
        db.commit()  # persist the rejected/_forbidden log line; nothing was mutated
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_17_04_error action=downgrade subscription_id=%s", subscription_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not process the downgrade"
        ) from exc
    db.commit()
    return _out(sub)


@router.post(
    "/{subscription_id}/cancel",
    response_model=SubscriptionActionResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Actor's role lacks manage_accounts."},
        status.HTTP_409_CONFLICT: {"description": "Ineligible for mid-term cancellation."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Cancellation failed; nothing was written."
        },
    },
)
def cancel(subscription_id: uuid.UUID, admin: AdminDep, db: DbDep) -> SubscriptionActionResponse:
    """FR-17-04/FR-17-07 — mid-term cancellation of a paid plan: no refund, access continues to
    `term_end_at` (left unchanged), tier stays premium. The actual drop to Free happens later, via
    `downgrade` above, once `term_end_at` has passed."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_accounts)
    except HTTPException:
        db.commit()
        logger.warning(
            "fr_17_04_forbidden action=cancel actor_id=%s subscription_id=%s",
            admin.id, subscription_id,
        )
        raise
    try:
        sub = downgrade_svc.cancel_subscription(db, subscription_id)
    except HTTPException:
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard: never leak a partial write
        db.rollback()
        logger.exception("fr_17_04_error action=cancel subscription_id=%s", subscription_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not process the cancellation"
        ) from exc
    db.commit()
    return _out(sub)
