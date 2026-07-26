"""FR-17-01: Free vs Premium entitlement engine — the single owner of tier state resolution
(ticket text: "the engine is the single owner of tier state; screens render entitlement, they do
not each re-decide it"). Reuses the `Subscription` model/table FR-02-02 + FR-19-02 already built —
this ticket adds no new persistence, only the tier -> feature-list mapping and its read endpoint.

Several Premium features listed here are delivered across Phase 2/3 (custom check-ins, AI
analysis, roster sync, calendar UI, school/district comparison, reports) — this ticket LISTS them
and gates on tier, it does not build them (ticket's own explicit out-of-scope line)."""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.constants.enums import SubscriptionTier
from src.domain.billing.services import get_or_create_subscription
from src.domain.identity.models import StaffAccount

logger = logging.getLogger("youhue.entitlements")

FREE_FEATURES: tuple[str, ...] = (
    "daily_checkins",
    "basic_mood_trends",
    "unlimited_students_and_classes",
    "colleague_invites",
    "young_child_simple_mode",
    "basic_activity_set",
)

PREMIUM_FEATURES: tuple[str, ...] = (
    "custom_checkins",
    "alerts_and_intervention_tracking",
    "ai_analysis",
    "richer_dashboards",
    "reports",
    "school_district_views",
    "roster_sync",
    "calendar",
    "access_windows",
    "onboarding",
)


def features_for_tier(tier: SubscriptionTier) -> list[str]:
    """The one formula every caller of this ticket's endpoint reads — no screen re-decides this."""
    if tier == SubscriptionTier.premium:
        return [*FREE_FEATURES, *PREMIUM_FEATURES]
    return list(FREE_FEATURES)


def get_entitlements(
    db: Session, staff: StaffAccount, school_id: uuid.UUID
) -> tuple[SubscriptionTier, list[str]]:
    """GET /api/v1/schools/{id}/entitlements — staff-only, school-scoped (403 out of scope, per
    DoD). Lazily creates the Subscription row on first read (same idempotent first-touch pattern
    `get_or_create_subscription` already established for FR-19-02) so a school with no explicit
    trial/approval action yet still resolves to the column defaults (free tier)."""
    if staff.school_id != school_id:
        logger.warning(
            "fr_17_01_forbidden action=get_entitlements staff_id=%s reason=cross_tenant "
            "school_id=%s", staff.id, school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    try:
        sub = get_or_create_subscription(db, school_id)
    except Exception as exc:
        db.rollback()
        logger.exception("fr_17_01_error action=get_entitlements school_id=%s", school_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve entitlements"
        ) from exc
    db.commit()
    features = features_for_tier(sub.tier)
    logger.info(
        "fr_17_01_success action=get_entitlements school_id=%s tier=%s", school_id, sub.tier.value
    )
    return sub.tier, features
