"""School leadership hub (FR-16-02) — leadership manages their OWN school's staff + the
leadership-exposed settings surfaces. This ticket's job is to STORE and SURFACE those settings via
ONE hub, never to build the engines that consume them (ticket §Out-of-scope):

  staff (SC-057)          — view + deactivate a staff account. Deactivation withdraws access
                             IMMEDIATELY with no separate revocation step: ``get_current_staff``
                             (infrastructure/middlewares/auth_middleware.py) already re-checks
                             ``StaffStatus.active`` on EVERY authenticated request, so flipping the
                             status to ``deactivated`` here is itself the revocation — the very next
                             request the deactivated staff member's existing session token makes is
                             denied 403, without touching ``AuthSession`` at all.
  concern words (SC-060)  — the school's OVERRIDE row on ``ConcernWordList``. FR-19-05 owns the
                             platform default; FR-12-01 is the sole consumer of the resolved list.
  alert routing (SC-061)  — ``AlertRecipientConfig`` rows. The escalation/order ENGINE is FR-12-05
                             (out of scope here) — this only stores the ordered recipient chain.
  access window (SC-063)  — ``CalendarConfig``. Window ENFORCEMENT + calendar-period resolution is
                             FR-07-03/FR-07-04 (out of scope here) — this only stores start/end/tz.

RBAC: ``StaffRole.leadership`` only, and same-school-only — the leadership actor's OWN school must
equal the ``school_id`` path param, or 403 (cross-tenant/non-leadership, INFRA-03 §8, no
exceptions).

Structured logs: ``fr_16_02_success`` / ``_rejected`` / ``_forbidden`` / ``_error`` (the ``_error``
log is written by the router around the one ACID transaction each write endpoint is).
"""
import logging
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.application.staff_lifecycle import services as staff_lifecycle
from src.constants.enums import StaffRole, StaffStatus
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount
from src.domain.org import services as org_db
from src.domain.risk import services as risk_db
from src.schemas.settings import (
    AccessWindowOut,
    AccessWindowSetting,
    AlertRoutingEntry,
    AlertRoutingEntryOut,
    ConcernWordsOut,
    SchoolSettingsOut,
    SchoolSettingsUpdate,
)

logger = logging.getLogger("youhue.leadership")

_ALLOWED_ALERT_TYPES = ("immediate", "triage")
MAX_WORDS = 1000
MAX_WORD_LEN = 100


def _require_leadership_same_school(
    actor: StaffAccount, school_id: uuid.UUID, *, action: str
) -> None:
    try:
        authz.require_roles(actor, StaffRole.leadership)
    except HTTPException:
        logger.warning("fr_16_02_forbidden action=%s actor_id=%s reason=role", action, actor.id)
        raise
    if actor.school_id != school_id:
        logger.warning(
            "fr_16_02_forbidden action=%s actor_id=%s reason=cross_tenant school_id=%s",
            action, actor.id, school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")


# --- SC-057: staff management --------------------------------------------------------------------


def list_staff(db: Session, actor: StaffAccount, school_id: uuid.UUID) -> list[StaffAccount]:
    _require_leadership_same_school(actor, school_id, action="list_staff")
    rows = identity_db.list_staff_in_school(db, school_id)
    logger.info(
        "fr_16_02_success action=list_staff actor_id=%s school_id=%s count=%s",
        actor.id, school_id, len(rows),
    )
    return rows


def deactivate_staff(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, staff_id: uuid.UUID, new_status: str
) -> StaffAccount:
    _require_leadership_same_school(actor, school_id, action="update_staff")

    if new_status != StaffStatus.deactivated.value:
        logger.warning(
            "fr_16_02_rejected action=update_staff actor_id=%s reason=invalid_status", actor.id
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported status value")

    # FR-02-04: row-locked for the duration of the transition — this endpoint and
    # `PATCH /staff/{id}/status` both mutate the same account's status, so a concurrent write from
    # either surface must not interleave read-then-write (see `get_staff_in_school_for_update`).
    target = identity_db.get_staff_in_school_for_update(db, school_id, staff_id)
    if target is None:
        logger.info(
            "fr_16_02_rejected action=update_staff actor_id=%s reason=not_found staff_id=%s",
            actor.id, staff_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Staff not found")

    # FR-02-04: routed through the one shared state machine (`staff_lifecycle.transition`) — same
    # idempotent-no-op-on-retry behaviour as before [BR-05], now backed by a real legal-transition
    # graph instead of an unconditional assignment.
    staff_lifecycle.transition(db, target, StaffStatus.deactivated)
    logger.info(
        "fr_16_02_success action=update_staff actor_id=%s staff_id=%s status=deactivated",
        actor.id, staff_id,
    )
    return target


# --- SC-060 / SC-061 / SC-063: the settings hub --------------------------------------------------


def _normalize_words(words: list[str]) -> list[str]:
    """Trim + lowercase, drop blanks, de-dupe (order-preserving) — mirrors FR-19-05's own
    normalization so a school override is canonical the same way the platform default is."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        w = raw.strip().lower()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _concern_words_out(db: Session, school_id: uuid.UUID) -> ConcernWordsOut:
    default_row = risk_db.get_default_concern_word_list(db)
    defaults = [w.lower() for w in default_row.words] if default_row is not None else []
    override_row = risk_db.get_concern_word_list(db, school_id)
    override_words = list(override_row.words) if override_row is not None else []
    default_set = set(defaults)
    additions = [w for w in override_words if w not in default_set]
    return ConcernWordsOut(platform_defaults=defaults, school_additions=additions)


def _snapshot(db: Session, school_id: uuid.UUID) -> SchoolSettingsOut:
    routing_rows = risk_db.get_alert_recipient_configs(db, school_id)
    window_row = org_db.get_calendar_config(db, school_id)
    return SchoolSettingsOut(
        concern_words=_concern_words_out(db, school_id),
        alert_routing=[
            AlertRoutingEntryOut(
                alert_type=r.alert_type, recipient_staff_ids=list(r.recipient_staff_ids)
            )
            for r in routing_rows
        ],
        access_window=(
            AccessWindowOut(
                window_start=window_row.window_start,
                window_end=window_row.window_end,
                timezone=window_row.timezone,
                term_start=window_row.term_start,
                term_end=window_row.term_end,
            )
            if window_row is not None
            else None
        ),
    )


def get_settings(db: Session, actor: StaffAccount, school_id: uuid.UUID) -> SchoolSettingsOut:
    _require_leadership_same_school(actor, school_id, action="read_settings")
    snap = _snapshot(db, school_id)
    logger.info(
        "fr_16_02_success action=read_settings actor_id=%s school_id=%s", actor.id, school_id
    )
    return snap


def update_settings(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, body: SchoolSettingsUpdate
) -> SchoolSettingsOut:
    _require_leadership_same_school(actor, school_id, action="update_settings")

    if body.concern_words is not None:
        _apply_concern_words(db, actor, school_id, body.concern_words.words)
    elif body.alert_routing is not None:
        _apply_alert_routing(db, actor, school_id, body.alert_routing.routes)
    elif body.access_window is not None:
        _apply_access_window(db, actor, school_id, body.access_window)
    else:  # pragma: no cover - SchoolSettingsUpdate's validator guarantees one field is set
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Exactly one setting is required")

    snap = _snapshot(db, school_id)
    logger.info(
        "fr_16_02_success action=update_settings actor_id=%s school_id=%s", actor.id, school_id
    )
    return snap


def _apply_concern_words(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, additions: list[str]
) -> None:
    """``additions`` = this school's own words on top of the (locked) platform default — see
    ``src/schemas/settings.py`` module docstring for why the persisted override row is the FULL
    resolved set (defaults ∪ additions), not the additions alone: ``resolve_words`` treats a school
    override as a REPLACEMENT of the default, so writing additions-only would silently drop the
    platform defaults for this school the moment it saves its first addition."""
    if len(additions) > MAX_WORDS or any(len(w) > MAX_WORD_LEN for w in additions):
        logger.warning(
            "fr_16_02_rejected action=update_settings setting=concern_words actor_id=%s", actor.id
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid concern-word list")

    cleaned_additions = _normalize_words(additions)
    default_row = risk_db.get_default_concern_word_list(db)
    defaults = [w.lower() for w in default_row.words] if default_row is not None else []
    full_list = _normalize_words(defaults + cleaned_additions)
    if not full_list:
        # An empty resolved list would disable concern-word detection for this school entirely.
        logger.warning(
            "fr_16_02_rejected action=update_settings setting=concern_words actor_id=%s "
            "reason=would_be_empty", actor.id,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The school's list must have at least one entry"
        )
    risk_db.set_school_concern_word_list(db, school_id, full_list)


def _apply_alert_routing(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, routes: list[AlertRoutingEntry]
) -> None:
    school_staff_ids = {s.id for s in identity_db.list_staff_in_school(db, school_id)}
    seen_types: set[str] = set()
    for entry in routes:
        if entry.alert_type not in _ALLOWED_ALERT_TYPES:
            logger.warning(
                "fr_16_02_rejected action=update_settings setting=alert_routing actor_id=%s "
                "reason=bad_alert_type", actor.id,
            )
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported alert type")
        if entry.alert_type in seen_types:
            logger.warning(
                "fr_16_02_rejected action=update_settings setting=alert_routing actor_id=%s "
                "reason=duplicate_alert_type", actor.id,
            )
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Duplicate alert type")
        seen_types.add(entry.alert_type)
        if not entry.recipient_staff_ids:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "At least one recipient is required"
            )
        if any(rid not in school_staff_ids for rid in entry.recipient_staff_ids):
            logger.warning(
                "fr_16_02_rejected action=update_settings setting=alert_routing actor_id=%s "
                "reason=cross_tenant_recipient", actor.id,
            )
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "Recipient must be staff at your school"
            )

    for entry in routes:
        risk_db.set_alert_recipient_config(
            db, school_id, entry.alert_type, entry.recipient_staff_ids
        )


def _apply_access_window(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, window: AccessWindowSetting
) -> None:
    if window.window_start >= window.window_end:
        logger.warning(
            "fr_16_02_rejected action=update_settings setting=access_window actor_id=%s "
            "reason=bad_range", actor.id,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Window start must be before end")
    try:
        ZoneInfo(window.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        logger.warning(
            "fr_16_02_rejected action=update_settings setting=access_window actor_id=%s "
            "reason=bad_timezone", actor.id,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown timezone") from exc

    # FR-07-04: term_start/term_end are optional and paired (schema-enforced); when both are
    # given, term_start must precede term_end — a school's term cannot end before it starts.
    if (
        window.term_start is not None
        and window.term_end is not None
        and window.term_start >= window.term_end
    ):
        logger.warning(
            "fr_16_02_rejected action=update_settings setting=access_window actor_id=%s "
            "reason=bad_term_range", actor.id,
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Term start must be before end")

    org_db.set_calendar_config(
        db,
        school_id,
        window.window_start,
        window.window_end,
        window.timezone,
        term_start=window.term_start,
        term_end=window.term_end,
    )
