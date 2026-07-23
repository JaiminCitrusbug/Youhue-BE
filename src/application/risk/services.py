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

from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.derived import services as derived
from src.constants.enums import FlagType
from src.domain.checkin import services as checkin_db
from src.domain.checkin.models import CheckIn
from src.domain.risk import services as risk_db

logger = logging.getLogger("youhue.risk")

CONCERN_WORD_SCORE = 0.90
SLOW_BURN_SCORE = 0.70


@dataclass
class ScoreResult:
    flagged: bool
    risk_score: float
    matched_terms: list[str]
    flag_id: uuid.UUID | None


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
