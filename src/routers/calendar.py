"""Calendar period resolution (FR-07-04): every dashboard 'this week/month/term' figure resolves
against the school's actual calendar + timezone through this ONE endpoint. Thin router — business
logic lives in ``src.application.calendar.services``. Staff-scoped to the CALLER's own school ONLY
(``staff.school_id`` off the authenticated session — never a request param, so it cannot be
spoofed cross-tenant). Read-only — no transaction to commit/roll back on the success path; an
unexpected failure surfaces a generic 500 and an ``fr_07_04_error`` log.
"""
import logging

from fastapi import APIRouter, HTTPException, status

from src.application.calendar import services as calendar_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.calendar import CalendarResolveOut, ErrorResponse

logger = logging.getLogger("youhue.calendar")
router = APIRouter(prefix="/calendar", tags=["calendar"])


@router.get(
    "/resolve",
    response_model=CalendarResolveOut,
    responses={
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorResponse,
            "description": "No calendar config (or, for this_term, no term dates) saved for "
            "this school yet.",
        },
        status.HTTP_422_UNPROCESSABLE_ENTITY: {
            "model": ErrorResponse,
            "description": "Unknown period_key.",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorResponse,
            "description": "Resolution failed.",
        },
    },
)
def resolve_calendar_period(
    period_key: str, staff: StaffDep, db: DbDep
) -> CalendarResolveOut:
    """Resolve ``period_key`` (``this_week`` | ``this_month`` | ``this_term``) to ``{start, end}``
    in the caller's school's configured timezone. ``end`` is EXCLUSIVE (start of the next
    period)."""
    try:
        start, end = calendar_svc.resolve_period(db, staff, period_key)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception("fr_07_04_error action=resolve")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Resolution failed") from exc
    return CalendarResolveOut(start=start, end=end)
