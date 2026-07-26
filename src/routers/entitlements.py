"""FR-17-01: Free vs Premium entitlement engine. Thin router — logic lives in
src.application.entitlements.services. Any authenticated staff at the school may read their own
school's entitlements (not leadership-only — every screen platform-wide gates on this)."""
import uuid

from fastapi import APIRouter, status

from src.application.entitlements import services as entitlements_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.entitlements import EntitlementsOut

router = APIRouter(prefix="/schools", tags=["entitlements"])


@router.get(
    "/{school_id}/entitlements",
    response_model=EntitlementsOut,
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Caller is staff at a different school."},
    },
)
def get_entitlements(school_id: uuid.UUID, staff: StaffDep, db: DbDep) -> EntitlementsOut:
    """SC-085 — the school's tier + the feature set it unlocks. The single source every gated
    feature/screen reads; nothing else re-decides tier."""
    tier, features = entitlements_svc.get_entitlements(db, staff, school_id)
    return EntitlementsOut(tier=tier.value, features=features)
