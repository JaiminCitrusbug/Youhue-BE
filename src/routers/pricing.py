"""FR-17-06 — informational, read-only pricing presentation (no PCI surface: no payment gateway,
no card vaulting, no payment webhooks; no card data taken or stored). Thin router — logic lives in
src.application.billing.pricing.

  GET /api/v1/pricing -> 200 {model, by_quote, tax} | 403 role not leadership/district
"""
from fastapi import APIRouter, status

from src.application.billing import pricing as pricing_svc
from src.infrastructure.middlewares.auth_middleware import StaffDep
from src.schemas.billing import PricingOut

router = APIRouter(prefix="/pricing", tags=["billing"])


@router.get(
    "",
    response_model=PricingOut,
    responses={
        status.HTTP_403_FORBIDDEN: {"description": "Role lacks leadership/district scope."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Pricing could not be resolved."},
    },
)
def get_pricing(staff: StaffDep) -> PricingOut:
    """SC-085 — the quote-based pricing model shown on the Subscription screen: per student per
    year, cheaper at higher volumes, sold by quote, no tax at launch. Presents the MODEL, never a
    ratified per-student number (BRD Appendix A, pending client ratification)."""
    data = pricing_svc.get_pricing(staff)
    return PricingOut(
        model=str(data["model"]), by_quote=bool(data["by_quote"]), tax=str(data["tax"])
    )
