"""Risk-scoring pipeline (INFRA-06) business logic. Async via the CheckIn.scored queue flag;
concern-word + slow-burn detectors. It produces a risk_score + Flag ONLY — it never decides the
action band (FR-12-06 routes) and never acts on a student (the AI surfaces, a human acts).
DB via domain services.
"""
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.authz import services as authz
from src.application.derived import services as derived
from src.application.notifications import services as notif_svc
from src.constants.enums import FlagBand, FlagEventType, FlagType, StaffRole
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import CheckIn
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount
from src.domain.risk import services as risk_db
from src.domain.risk.models import AlertRecipientConfig, Flag

logger = logging.getLogger("youhue.risk")

CONCERN_WORD_SCORE = 0.90
SLOW_BURN_SCORE = 0.70
_ALLOWED_ALERT_TYPES = ("immediate", "triage")


@dataclass
class ScoreResult:
    flagged: bool
    risk_score: float
    matched_terms: list[str]
    flag_id: uuid.UUID | None


@dataclass
class GuidanceContent:
    suggested_wording: str
    next_steps: list[str]
    links: list[str]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@derived.owns("flag.risk_score")
def combine_risk_score(concern_score: float, slow_burn_score: float) -> float:
    """Single owner of flag.risk_score (§13.5): a check-in's score is its strongest detector hit."""
    return max(concern_score, slow_burn_score)


def resolve_words(db: Session, school_id: uuid.UUID) -> list[str]:
    """School override list where present (even if empty — a school may opt out), else the
    admin-maintained platform DEFAULT (FR-19-05), else the env seed. GATE G-6: a school's own
    override always wins, so changing the default never affects a school that has overridden it."""
    row = risk_db.get_concern_word_list(db, school_id)
    if row is not None:
        return [w.lower() for w in row.words]
    default = risk_db.get_default_concern_word_list(db)
    if default is not None:
        return [w.lower() for w in default.words]
    return settings.concern_words  # env seed until the internal team ratifies a default


def _word_present(word: str, text: str) -> bool:
    """Whole-word match so 'help' does not fire on 'helpful', while multi-word phrases still match;
    limits noise without tuning toward silence (ticket §Must-nots: bias to flagging)."""
    word = word.strip().lower()
    if not word:
        return False
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text) is not None


def detect_concern_word(reflection: str | None, words: list[str]) -> tuple[list[str], float]:
    if not reflection:
        return [], 0.0
    text = reflection.lower()
    matched = [w for w in words if _word_present(w, text)]
    return matched, (CONCERN_WORD_SCORE if matched else 0.0)


def detect_slow_burn(db: Session, student_id: uuid.UUID) -> float:
    """Low mood sustained across >= the configured number of DISTINCT days within the window flags;
    a recovered trend (most recent check-in risen back above the low threshold) clears it. Counting
    distinct low days means a single mid-window blip does NOT suppress a real streak — the detector
    biases toward flagging over missing (ticket §Must-nots), while a genuine recovery still clears.
    """
    since = _utcnow() - timedelta(days=settings.slowburn_window_days)
    recent = checkin_db.get_checkins_since(db, student_id, since)
    if not recent:
        return 0.0
    low = settings.slowburn_low_mood_threshold
    latest = max(recent, key=lambda c: c.submitted_at)
    if latest.mood_value > low:
        return 0.0  # recovered — the most recent mood is back above the low threshold
    low_days = {c.submitted_at.date() for c in recent if c.mood_value <= low}
    if len(low_days) >= settings.slowburn_min_low_days:
        return SLOW_BURN_SCORE
    return 0.0


def score_checkin(db: Session, checkin: CheckIn) -> ScoreResult:
    """Score one check-in; create a Flag if warranted. Idempotent per check-in (retry-safe).
    Produces score + flag ONLY — never decides a band (null until FR-12-06 routes) or acts."""
    existing = risk_db.get_flag_by_checkin(db, checkin.id) if checkin.id else None
    if existing is not None:  # already scored+flagged -> idempotent, do not duplicate the flag
        checkin_db.mark_scored(db, checkin)
        return ScoreResult(True, float(existing.risk_score), [], existing.id)

    words = resolve_words(db, checkin.school_id)
    matched, cw_score = detect_concern_word(checkin.reflection_text, words)
    sb_score = detect_slow_burn(db, checkin.student_id)
    risk_score = combine_risk_score(cw_score, sb_score)

    checkin_db.mark_scored(db, checkin)
    if matched:
        flag_type = FlagType.concern_word
    elif sb_score >= settings.risk_triage_threshold:
        flag_type = FlagType.slow_burn
    else:
        logger.info("fr_12_01_success checkin=%s flagged=0", checkin.id)
        return ScoreResult(False, risk_score, [], None)

    flag = risk_db.create_flag(
        db,
        student_id=checkin.student_id,
        school_id=checkin.school_id,
        checkin_id=checkin.id,
        flag_type=flag_type,
        risk_score=risk_score,  # band left null -> FR-12-06 decides the action band
    )
    logger.info("fr_12_01_success checkin=%s flagged=1 type=%s", checkin.id, flag_type.value)
    return ScoreResult(True, risk_score, matched, flag.id)


def evaluate_slow_burn(db: Session, student_id: uuid.UUID, school_id: uuid.UUID) -> bool:
    """FR-12-03: on-demand/scheduled slow-burn evaluation over a student's EXISTING check-in
    history — independent of any single submission (unlike `score_checkin`, which only evaluates
    the slow-burn trend inline when a NEW check-in lands). Lets a scheduled system process catch a
    quiet decline even on a day the student doesn't check in at all.

    Idempotent under real concurrency, not just sequential retry: a checkin_id-keyed uniqueness
    constraint doesn't apply here (this call has no checkin to anchor to — checkin_id is left null
    on the created flag), so the actual backstop is `upsert_open_flag`'s DB-level partial unique
    index + ON CONFLICT DO NOTHING + SELECT ... FOR UPDATE convergence — see its docstring.
    """
    if risk_db.get_open_flag(db, student_id, FlagType.slow_burn) is not None:
        return True  # cheap fast path only — the real race-safety is upsert_open_flag below
    score = detect_slow_burn(db, student_id)
    if score < settings.risk_triage_threshold:
        return False
    risk_db.upsert_open_flag(
        db,
        student_id=student_id,
        school_id=school_id,
        flag_type=FlagType.slow_burn,
        risk_score=score,
    )
    return True


def set_alert_config(
    db: Session,
    actor: StaffAccount,
    school_id: uuid.UUID,
    alert_type: str,
    recipient_staff_ids: list[uuid.UUID],
) -> AlertRecipientConfig:
    """FR-12-05: PUT /api/v1/schools/{id}/alert-config — leadership sets the ordered recipient
    chain for one alert_type; a flag has no route until this exists (GATE G-5). Leadership-only,
    school-scoped (BR-01); recipients must be staff at the caller's own school. Shares the
    underlying table/write path with FR-16-02's settings PATCH
    (`risk_db.set_alert_recipient_config`, now DB-upsert-backed — see its docstring) but owns its
    OWN dedicated endpoint contract and log namespace, per this ticket's explicit DoD."""
    try:
        authz.require_roles(actor, StaffRole.leadership)
    except HTTPException:
        logger.warning(
            "fr_12_05_forbidden action=set_alert_config actor_id=%s reason=role", actor.id
        )
        raise
    if actor.school_id != school_id:
        logger.warning(
            "fr_12_05_forbidden action=set_alert_config actor_id=%s reason=cross_tenant "
            "school_id=%s", actor.id, school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    if alert_type not in _ALLOWED_ALERT_TYPES:
        logger.warning(
            "fr_12_05_rejected action=set_alert_config actor_id=%s reason=bad_alert_type", actor.id
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unsupported alert type")
    school_staff_ids = {s.id for s in identity_db.list_staff_in_school(db, school_id)}
    if any(rid not in school_staff_ids for rid in recipient_staff_ids):
        logger.warning(
            "fr_12_05_rejected action=set_alert_config actor_id=%s "
            "reason=cross_tenant_recipient", actor.id,
        )
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "Recipient must be staff at your school"
        )
    row = risk_db.set_alert_recipient_config(db, school_id, alert_type, recipient_staff_ids)
    logger.info(
        "fr_12_05_success action=set_alert_config actor_id=%s school_id=%s alert_type=%s",
        actor.id, school_id, alert_type,
    )
    return row


def decide_band(risk_score: float) -> FlagBand:
    """FR-12-06: single owner of the band decision (render-only downstream). Cut-offs are
    environment-configurable (`settings.risk_immediate_threshold` / `risk_triage_threshold`, D-05
    pending BA/SA ratification — a config-driven default, same posture as every other
    pending-ratification cut-off this project has shipped) and default to erring toward triage over
    silence: the triage threshold is the lower, more inclusive bound, so an ambiguous score lands in
    triage (human review) rather than falling through to no action."""
    if risk_score >= settings.risk_immediate_threshold:
        return FlagBand.immediate
    if risk_score >= settings.risk_triage_threshold:
        return FlagBand.triage
    return FlagBand.none


def dispatch_alert(db: Session, flag: Flag) -> int:
    """FR-12-04 (GATE G-7): hand off an immediate-band flag to its school's configured adults by
    email + in-app — ENQUEUES only (creates the Notification + per-channel AlertDelivery rows via
    the existing INFRA-05/FR-18-03 path); it does NOT itself send email or run the retry engine
    (owned by `process_due_deliveries` — reused, not owned, per this ticket's own out-of-scope
    line). Called inline from `route_checkin` (FR-12-06) on an immediate-band routing decision, and
    from `POST /api/v1/alerts/dispatch` for a standalone/system-triggered dispatch of an
    already-routed flag.

    Idempotent per flag (Baseline BR-05): a flag that already has an `alerted` FlagEvent is not
    re-enqueued on retry (`has_flag_event`) — a retry never double-alerts. Recipients resolve ONLY
    from the school's own FR-12-05 configuration (never invented) — dispatch is school-scoped
    (BR-01). No configured route (FR-12-05 not yet set for this school/alert_type, GATE G-5) means
    zero recipients — a legitimate, non-error state, not a 500."""
    if risk_db.has_flag_event(db, flag.id, FlagEventType.alerted):
        logger.info("fr_12_04_success flag=%s recipients=0 reason=already_alerted", flag.id)
        return 0
    config = risk_db.get_alert_recipient_config(db, flag.school_id, "immediate")
    if config is None or not config.recipient_staff_ids:
        logger.info("fr_12_04_success flag=%s recipients=0 reason=no_route", flag.id)
        return 0
    for recipient_id in config.recipient_staff_ids:
        notif_svc.enqueue(
            db,
            recipient_id=recipient_id,
            ntype="risk_alert",
            payload={"flag_id": str(flag.id), "band": "immediate", "reason": flag.type.value},
            flag_id=flag.id,
        )
    risk_db.record_flag_alerted(db, flag.id)
    logger.info(
        "fr_12_04_success flag=%s school=%s recipients=%s",
        flag.id, flag.school_id, len(config.recipient_staff_ids),
    )
    return len(config.recipient_staff_ids)


def route_checkin(db: Session, checkin_id: uuid.UUID) -> tuple[FlagBand, uuid.UUID | None]:
    """FR-12-06: route a scored check-in to exactly one band. GATE G-9 — on completion this ONLY
    persists `Flag.band` and (for immediate) enqueues a notification to already-configured adults;
    it never writes to Student, never messages/locks/notifies the child, and takes no other action.

    Idempotent (Baseline BR-05): a check-in with no Flag (score never cleared the flagging
    threshold — see `score_checkin`) is already, implicitly, `none` — nothing to route, no Flag row
    needed. A Flag whose band is already set returns the existing decision unchanged (a retry never
    re-alerts)."""
    flag = risk_db.get_flag_by_checkin(db, checkin_id)
    if flag is None:
        return FlagBand.none, None
    if flag.band is not None:
        return flag.band, flag.id
    band = decide_band(float(flag.risk_score))
    risk_db.set_flag_band(db, flag, band)
    if band == FlagBand.immediate:
        dispatch_alert(db, flag)  # FR-12-04: hand off to the school's configured adults
    logger.info("fr_12_06_success checkin=%s flag=%s band=%s", checkin_id, flag.id, band.value)
    return band, flag.id


def process_pending(db: Session) -> int:
    """Worker pass: score unscored check-ins with a per-item commit so one poison item never sinks
    the batch. A scoring error is retried (bounded) and, once the cap is hit, dead-lettered and
    surfaced CRITICAL — never silently dropped (ticket §Must-nots)."""
    pending = checkin_db.get_unscored_for_update(db)
    processed = 0
    for c in pending:
        checkin_id = c.id
        try:
            score_checkin(db, c)
            db.commit()
            processed += 1
        except Exception:  # noqa: BLE001 - one failing item must not poison the batch
            db.rollback()
            fresh = checkin_db.get_checkin(db, checkin_id)
            if fresh is None:
                continue
            attempts = checkin_db.bump_score_attempt(db, fresh)
            if attempts >= settings.max_score_attempts:
                checkin_db.mark_scored(db, fresh)  # dead-letter: stop retrying, stays surfaced
                logger.critical(
                    "fr_12_01_error checkin=%s attempts=%s dead_letter=1", checkin_id, attempts
                )
            else:
                logger.error(
                    "fr_12_01_error checkin=%s attempts=%s dead_letter=0", checkin_id, attempts
                )
            db.commit()
    return processed


def build_guidance(flag: Flag) -> GuidanceContent:
    """FR-13-04: pure, derive-only guidance content keyed off the flag's OWN detector type + band
    — no persistence, no student action, no invented clinical directive. Advisory copy only: every
    next step reads as a suggestion the teacher may use, adapt or ignore (ticket §Must-nots), never
    a mandated action."""
    if flag.type == FlagType.concern_word:
        context = "something in their check-in stood out to you"
    else:  # slow_burn
        context = "their mood has stayed low for a few days"
    suggested_wording = (
        f'"Hi, I noticed {context} — I just wanted to check in. How are you doing, really?"'
    )
    next_steps = [
        "Find a quiet, low-pressure moment to check in — not in front of peers.",
        "Listen first; you don't need to have all the answers.",
        "Let your school's designated safeguarding lead know what you noticed.",
    ]
    if flag.band == FlagBand.immediate:
        next_steps.append("Given the urgency, consider following up the same day if you can.")
    links = [
        "School safeguarding steps",
        "Talking with a student who seems low",
        "When and how to escalate",
    ]
    return GuidanceContent(suggested_wording=suggested_wording, next_steps=next_steps, links=links)


def get_guidance(db: Session, staff: StaffAccount, flag_id: uuid.UUID) -> GuidanceContent:
    """FR-13-04 (SC-040): advisory guidance for the teacher opening the response to a flagged
    check-in. Read restricted to the INVOLVED teacher (403 otherwise) — reuses FR-10-02's
    `authz.require_student_access` (own/shared class scope), the established "who is involved with
    this student" primitive in this codebase; there is no separate per-flag assignment concept to
    invent. Same three-tier error shape as `students_svc.get_student_detail`: unknown flag -> 404,
    cross-tenant -> 403, out-of-scope teacher -> 403. Read-only: no write, no commit, no action."""
    flag = risk_db.get_flag(db, flag_id)
    if flag is None:
        logger.info(
            "fr_13_04_rejected action=get_guidance actor_id=%s reason=not_found flag_id=%s",
            staff.id, flag_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Flag not found")
    if flag.school_id != staff.school_id:
        logger.warning(
            "fr_13_04_forbidden action=get_guidance actor_id=%s reason=cross_tenant flag_id=%s",
            staff.id, flag_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    student = identity_db.get_student(db, flag.student_id)
    if student is None:  # pragma: no cover - a flag's own student is never deleted from under it
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    try:
        authz.require_student_access(db, staff, student)  # the "involved teacher" check
    except HTTPException:
        logger.warning(
            "fr_13_04_forbidden action=get_guidance actor_id=%s reason=not_involved flag_id=%s",
            staff.id, flag_id,
        )
        raise
    guidance = build_guidance(flag)
    logger.info(
        "fr_13_04_success action=get_guidance actor_id=%s flag_id=%s", staff.id, flag.id
    )
    return guidance
