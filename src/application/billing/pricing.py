"""FR-17-06 — informational, read-only pricing presentation. Per student per year, cheaper at
higher volumes, sold by quote; no tax at launch; no self-service card entry anywhere in the
platform (NEG, the negative this ticket exists to protect — no payment gateway, no card vaulting,
no payment webhooks: no PCI surface, no card data taken or stored, here or anywhere else in this
module).

The recommended per-student price bands are pending client ratification (BRD Appendix A) — this
module presents the MODEL only, never a ratified number. The three literal values below (the
model name, the by-quote flag, the no-tax-at-launch note) ARE that model, verbatim from the
ticket's own DoD line — not a price, so hard-coding them is the ticket's own contract, not the
"hard-coded ratified number" its Do-NOT line forbids.

Leadership/district scope (ticket DoD: "leadership/district scope") — the roles that review
pricing when a school moves from trial to a paid plan (ticket Scenario 1); teacher/support have no
reason to see it and are refused, same `authz.require_roles` posture as every other role-scoped
GET in this codebase. No school-scoping check is needed beyond that: the model is platform-wide,
not school-specific data (unlike entitlements/downgrade, which act on one school's own row).
"""
import logging

from fastapi import HTTPException, status

from src.application.authz import services as authz
from src.constants.enums import StaffRole
from src.domain.identity.models import StaffAccount

logger = logging.getLogger("youhue.billing")

_ALLOWED_ROLES = (StaffRole.leadership, StaffRole.district)

# token-ok: not a UI value — the literal API contract fixed by the ticket's own DoD line, not a
# ratified price (BRD Appendix A price bands remain unset, pending client ratification).
PRICING_MODEL: dict[str, str | bool] = {
    "model": "per_student_per_year",
    "by_quote": True,
    "tax": "none at launch",
}


def get_pricing(staff: StaffAccount) -> dict[str, str | bool]:
    """GET /api/v1/pricing — the quote-based pricing model. Takes no card data, stores none, and
    a 500 here is surfaced (never silently dropped), matching every other endpoint in this
    module."""
    try:
        authz.require_roles(staff, *_ALLOWED_ROLES)
    except HTTPException:
        logger.warning(
            "fr_17_06_forbidden action=get_pricing actor_id=%s role=%s",
            staff.id, staff.role.value,
        )
        raise
    try:
        data = dict(PRICING_MODEL)
    except Exception as exc:  # noqa: BLE001 - last-resort guard: a 500 is surfaced, never dropped
        logger.exception("fr_17_06_error action=get_pricing actor_id=%s", staff.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve pricing"
        ) from exc
    logger.info("fr_17_06_success action=get_pricing actor_id=%s", staff.id)
    return data
