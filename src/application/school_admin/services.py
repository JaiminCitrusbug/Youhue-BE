"""Admin console — school accounts, ONE 14-day trial extension, and permission-bound, audited
support access to a school's children's data (FR-19-02 / SC-075 / SC-076 / SC-077).

Reuses the FR-19-01 admin RBAC policy layer (`application/authz/admin.py`) — no parallel auth is
built here. One PATCH endpoint, three actions:

  extend_trial    `manage_accounts`-gated. Extends the school's trial by 14 days — but ONLY ONCE
                   EVER: `Subscription.trial_extension_count` enforces it, a second attempt is
                   refused (409 "extension already used"). Lives on `Subscription` (see
                   `src/domain/billing/services.py` docstring for why/where).
  update_account  `manage_accounts`-gated. Applies the given account/trial-setting changes
                   (tier / status / timezone) to the target school.
  support_access  `access_child_data`-gated (its OWN, narrower permission — see
                   `authz/admin.py`). Every access is audit-logged (actor_id, action, target,
                   timestamp) before it is granted — permission-bound and recorded, never an open
                   backdoor. Requires a non-blank reason (matches the approved SC-077 screen).

Every action writes an immutable `AuditLog` row (`fr_19_02.<action>`, `target=school:<id>`,
`school_id=<id>`) — the RBAC denial path audits FIRST (via `require_permission`, reused as-is) so a
denied attempt is on the record too. The router commits so a denial's audit row survives the raised
HTTPException; business-action rows are committed by the router on success.
"""
import logging
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import admin as admin_authz
from src.constants.enums import SchoolStatus, SchoolTier
from src.domain.billing import services as billing_db
from src.domain.billing.models import Subscription
from src.domain.compliance import services as audit_db
from src.domain.identity import services as identity_db
from src.domain.identity.models import InternalAdmin, School

logger = logging.getLogger("youhue.admin.schools")

TRIAL_EXTENSION_DAYS = 14


def _get_school(db: Session, school_id: uuid.UUID) -> School:
    school = identity_db.get_school(db, school_id)
    if school is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "School not found")
    return school


def _audit(db: Session, admin: InternalAdmin, action: str, school: School) -> None:
    audit_db.write_audit(
        db,
        actor_id=admin.id,
        action=f"fr_19_02.{action}",
        target=f"school:{school.id}",
        school_id=school.id,
    )


def list_schools(db: Session, admin: InternalAdmin, q: str | None = None) -> list[School]:
    """SC-075 — every school on the platform (name / tier / status). `manage_accounts`-gated."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_accounts)
    except HTTPException:
        logger.warning("fr_19_02_forbidden admin=%s action=list_schools", admin.id)
        raise

    return identity_db.list_schools(db, name_contains=q)


def get_school(
    db: Session, admin: InternalAdmin, school_id: uuid.UUID
) -> tuple[School, Subscription | None]:
    """SC-076/077 detail read — same `manage_accounts` gate as the list."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_accounts)
    except HTTPException:
        logger.warning("fr_19_02_forbidden admin=%s action=get_school", admin.id)
        raise

    school = _get_school(db, school_id)
    sub = billing_db.get_subscription(db, school_id)
    return school, sub


def extend_trial(
    db: Session, admin: InternalAdmin, school_id: uuid.UUID
) -> tuple[School, Subscription]:
    """SC-076 — grant the school's ONE 14-day trial extension. A second attempt is refused (409)."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_accounts)
    except HTTPException:
        logger.warning("fr_19_02_forbidden admin=%s action=extend_trial", admin.id)
        raise

    school = _get_school(db, school_id)
    sub = billing_db.get_or_create_subscription(db, school_id)
    if sub.trial_extension_count >= 1:
        logger.warning(
            "fr_19_02_extension_reused admin=%s school=%s", admin.id, school.id
        )
        raise HTTPException(status.HTTP_409_CONFLICT, "extension already used")

    base = sub.trial_end_at or datetime.now(UTC)
    sub.trial_end_at = base + timedelta(days=TRIAL_EXTENSION_DAYS)
    sub.trial_extension_count += 1
    _audit(db, admin, "extend_trial", school)
    logger.info("fr_19_02_success admin=%s action=extend_trial school=%s", admin.id, school.id)
    return school, sub


def update_account(
    db: Session,
    admin: InternalAdmin,
    school_id: uuid.UUID,
    *,
    tier: SchoolTier | None = None,
    account_status: SchoolStatus | None = None,
    timezone: str | None = None,
) -> tuple[School, Subscription | None]:
    """SC-075 — apply the given account/trial-setting changes to the target school."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_accounts)
    except HTTPException:
        logger.warning("fr_19_02_forbidden admin=%s action=update_account", admin.id)
        raise

    school = _get_school(db, school_id)
    if tier is not None:
        school.tier = tier
    if account_status is not None:
        school.status = account_status
    if timezone is not None:
        school.timezone = timezone
    _audit(db, admin, "update_account", school)
    logger.info("fr_19_02_success admin=%s action=update_account school=%s", admin.id, school.id)
    sub = billing_db.get_subscription(db, school_id)
    return school, sub


def support_access(
    db: Session, admin: InternalAdmin, school_id: uuid.UUID, *, reason: str | None
) -> tuple[School, Subscription | None]:
    """SC-077 — internal-team support access to a school's children's data.
    `access_child_data`-gated (a role without it -> 403, audit-logged before the raise) and every
    granted access is itself audit-logged — permission-bound and recorded, never an open backdoor.
    Requires a non-blank reason (matches the approved screen's "Reason for access" field); the
    reason text is NOT persisted into the immutable audit row (kept to the ticket's literal
    actor_id/action/target/timestamp shape — no free-text field on that table, same
    don't-log-content precedent FR-19-05 set for word content)."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.access_child_data)
    except HTTPException:
        logger.warning("fr_19_02_forbidden admin=%s action=support_access", admin.id)
        raise

    school = _get_school(db, school_id)
    if not reason or not reason.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "A reason is required to open support access"
        )

    _audit(db, admin, "support_access", school)
    logger.info("fr_19_02_success admin=%s action=support_access school=%s", admin.id, school.id)
    sub = billing_db.get_subscription(db, school_id)
    return school, sub
