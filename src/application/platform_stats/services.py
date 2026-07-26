"""Platform-level statistics (FR-19-07, SC-074) — read-only aggregates over base tables for the
internal admin dashboard. Computes NO new domain state; every figure is a plain COUNT over an
existing, single-owner table (School / Subscription / CheckIn / Flag), rendered not recomputed
downstream [Baseline BR-05]. No per-school or per-child data is ever returned — platform aggregates
only (isolation, FR-20-07)."""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.application.authz import admin as admin_authz
from src.domain.identity.models import InternalAdmin
from src.domain.platform_stats import services as stats_db

logger = logging.getLogger("youhue.admin.stats")


@dataclass
class PlatformStats:
    schools: int
    active_trials: int
    checkin_volume: int
    alert_volume: int


def _now() -> datetime:
    return datetime.now(UTC)


def get_platform_stats(db: Session, admin: InternalAdmin) -> PlatformStats:
    """`view_statistics`-gated (a role without it -> 403, audit-logged by `require_permission`).
    An empty platform (no schools/trials/check-ins/alerts yet) returns all-zero counts — a valid,
    renderable state, not an error; the caller (router) never turns a zero into a 500."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.view_statistics)
    except HTTPException:
        logger.warning("fr_19_07_forbidden admin=%s action=get_platform_stats", admin.id)
        raise

    stats = PlatformStats(
        schools=stats_db.count_schools(db),
        active_trials=stats_db.count_active_trials(db, _now()),
        checkin_volume=stats_db.count_checkins(db),
        alert_volume=stats_db.count_flags(db),
    )
    logger.info(
        "fr_19_07_success admin=%s schools=%s active_trials=%s checkin_volume=%s alert_volume=%s",
        admin.id, stats.schools, stats.active_trials, stats.checkin_volume, stats.alert_volume,
    )
    return stats
