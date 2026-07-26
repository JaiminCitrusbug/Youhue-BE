"""FR-10-01 — the class dashboard: a teacher's weighted class mood index + trend (SRS §13.5
single-owner derived values, registered below). Owns `class.mood_index` / `class.trend` — no other
module may compute these; every consumer (the FE, any future report) reads this endpoint's output
and renders it, never recomputing.

D-18 (the actual weighting scheme) is UNRATIFIED (ticket §Must-nots: "the figure is owned
server-side... Do NOT ratify the D-18 weighting formula here — consume the owner's value"). This
module IS the owner, so it must still return SOMETHING today; the interim formula (unweighted mean
of in-window `CheckIn.mood_value`, config-scaled) is deliberately simple and entirely
config-driven (`settings.dashboard_mood_index_scale`), matching this codebase's established
placeholder-pending-ratification posture (`default_concern_words`, the FR-04-01 mood set) — never
hard-coded as if it were a ratified product decision.
"""
import logging
import statistics
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.authz import services as authz
from src.application.calendar import services as calendar_svc
from src.application.derived import services as derived
from src.domain.checkin import services as checkin_db
from src.domain.identity.models import StaffAccount
from src.domain.org import services as org_db
from src.domain.org.models import ClassGroup
from src.schemas.dashboard import ClassDashboardOut, ClassRosterOut, ClassRosterRow

logger = logging.getLogger("youhue.dashboard")

_DEFAULT_PERIOD_KEY = "this_week"  # FR-10-03 (time-range filter) is a later ticket; this ticket
# ships the one period the approved screen defaults to ("This week...").


@derived.owns("class.mood_index")
def compute_mood_index(mood_values: list[int]) -> float | None:
    """The dashboard's single-owner mood-index formula (unweighted mean, config-scaled). Returns
    `None` for an empty window — the caller decides how to render "no data yet", never a fabricated
    0."""
    if not mood_values:
        return None
    return round(statistics.mean(mood_values) * settings.dashboard_mood_index_scale, 2)


@derived.owns("class.trend")
def compute_trend(current_index: float | None, prior_index: float | None) -> str:
    """The dashboard's single-owner trend formula: compares the CURRENT period's mood index
    against the PRIOR period's. Either side missing (no check-ins yet in that window) reports
    "flat" — there is no meaningful direction to report against an empty comparator, and a
    fabricated up/down would mislead a teacher acting on it."""
    if current_index is None or prior_index is None:
        return "flat"
    delta = current_index - prior_index
    if abs(delta) < settings.dashboard_trend_flat_threshold:
        return "flat"
    return "up" if delta > 0 else "down"


def get_class_dashboard(db: Session, staff: StaffAccount, class_id: uuid.UUID) -> ClassDashboardOut:
    # `require_class_access` 403s for BOTH "not this staff member's own/shared class" AND "no such
    # class" (a nonexistent id resolves to no accessible class either way) — deliberately never
    # distinguishes the two responses, so a caller cannot probe for another school's class ids.
    authz.require_class_access(db, staff, class_id)
    # `require_class_access` above already guarantees a real, accessible class row exists.
    klass = cast(ClassGroup, org_db.get_class(db, class_id))

    config = org_db.get_calendar_config(db, staff.school_id)
    start, end = calendar_svc.resolve_period(db, staff, _DEFAULT_PERIOD_KEY)
    period_length = end - start
    prior_start, prior_end = start - period_length, start

    student_ids = org_db.get_student_ids_in_class(db, class_id)
    current_checkins = checkin_db.list_checkins_for_students_between(db, student_ids, start, end)
    prior_checkins = checkin_db.list_checkins_for_students_between(
        db, student_ids, prior_start, prior_end
    )

    current_index = derived.compute(
        "class.mood_index", [c.mood_value for c in current_checkins]
    )
    prior_index = derived.compute("class.mood_index", [c.mood_value for c in prior_checkins])
    trend = derived.compute("class.trend", current_index, prior_index)

    logger.info(
        "fr_10_01_success action=get_dashboard actor_id=%s class_id=%s mood_index=%s trend=%s",
        staff.id, class_id, current_index, trend,
    )
    return ClassDashboardOut(
        class_id=str(class_id),
        class_name=klass.name,
        mood_index=current_index,
        trend=trend,
        as_of=datetime.now(UTC),
        live=True,  # the dashboard always computes on-demand, never from a stale cache
        period=_DEFAULT_PERIOD_KEY,
        timezone=config.timezone if config is not None else "UTC",
    )


def get_class_roster(db: Session, staff: StaffAccount, class_id: uuid.UUID) -> ClassRosterOut:
    """FR-10-02: the class's real student rows, same own/shared-class scoping as the dashboard
    itself (`require_class_access` — existence hidden, same 403 for "not yours" and "no such
    class")."""
    authz.require_class_access(db, staff, class_id)
    rows = org_db.list_students_in_class(db, class_id)
    return ClassRosterOut(
        students=[ClassRosterRow(id=s.id, display_name=s.display_name) for s in rows]
    )
