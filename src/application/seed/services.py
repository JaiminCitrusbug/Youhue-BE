"""Seed activity-set business logic (FR-19-04) — internal-team maintenance of the platform default
activity library.

The internal team creates / edits / lists / retires the seed activities that ship to EVERY school
(scope=seed, school_id=NULL — global, never school-scoped). FR-05-01 (post-check-in guided activity)
and FR-14-02 (teacher run/assign) later READ this active set; they are NOT built here.

Every mutating action is gated by the admin RBAC `manage_seed` permission (FR-19-01 policy layer):
a role without it gets 403 and the denial is audit-logged first (never an open backdoor). Successful
maintenance is also written to the immutable audit log. This module owns authorization + audit +
orchestration; raw DB access lives in `src.domain.checkin.services`.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import admin as admin_authz
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import Activity
from src.domain.compliance import services as audit_db
from src.domain.identity.models import InternalAdmin
from src.schemas.admin import SeedActivityCreate, SeedActivityUpdate

_PERMISSION = admin_authz.AdminPermission.manage_seed


def _audit(db: Session, admin: InternalAdmin, action: str, target: str) -> None:
    audit_db.write_audit(
        db, actor_id=admin.id, action=action, target=target, school_id=None
    )


def _load_or_404(db: Session, activity_id: uuid.UUID) -> Activity:
    activity = checkin_db.get_seed_activity(db, activity_id)
    if activity is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Seed activity not found")
    return activity


def create_seed_activity(
    db: Session, admin: InternalAdmin, payload: SeedActivityCreate
) -> Activity:
    """Add an activity to the seed set available to all schools. Gated by `manage_seed`."""
    admin_authz.require_permission(db, admin, _PERMISSION)
    activity = checkin_db.add_seed_activity(
        db,
        title=payload.title,
        type=payload.type,
        age_band=payload.age_band,
        topic=payload.topic,
    )
    _audit(db, admin, "admin.seed.created", str(activity.id))
    return activity


def list_seed_activities(
    db: Session, admin: InternalAdmin, *, include_retired: bool = False
) -> list[Activity]:
    """List the seed set for the admin console. Gated by `manage_seed`."""
    admin_authz.require_permission(db, admin, _PERMISSION)
    return checkin_db.list_seed_activities(db, include_retired=include_retired)


def edit_seed_activity(
    db: Session, admin: InternalAdmin, activity_id: uuid.UUID, payload: SeedActivityUpdate
) -> Activity:
    """Edit a seed activity's fields. Unknown / school-scoped id -> 404. Gated by `manage_seed`."""
    admin_authz.require_permission(db, admin, _PERMISSION)
    activity = _load_or_404(db, activity_id)
    changes = payload.model_dump(exclude_unset=True)
    for field, value in changes.items():
        setattr(activity, field, value)
    db.flush()
    _audit(db, admin, "admin.seed.edited", str(activity.id))
    return activity


def retire_seed_activity(
    db: Session, admin: InternalAdmin, activity_id: uuid.UUID
) -> Activity:
    """Retire (soft-remove) a seed activity so schools no longer see it. Gated by `manage_seed`."""
    admin_authz.require_permission(db, admin, _PERMISSION)
    activity = _load_or_404(db, activity_id)
    activity.active = False
    db.flush()
    _audit(db, admin, "admin.seed.retired", str(activity.id))
    return activity
