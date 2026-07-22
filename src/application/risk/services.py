"""Risk-scoring pipeline (INFRA-06) business logic. Async via the CheckIn.scored queue flag;
concern-word + slow-burn detectors; surfaces signal, never acts on a student. DB via domain.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.derived import services as derived
from src.constants.enums import FlagBand, FlagType
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


def resolve_words(db: Session, school_id: uuid.UUID) -> list[str]:
    """School override list where present (even if empty — a school may opt out), else default."""
    row = risk_db.get_concern_word_list(db, school_id)
    if row is not None:
        return [w.lower() for w in row.words]
    return settings.concern_words


@derived.owns("flag.band")
def route_band(score: float) -> FlagBand:
    if score >= settings.risk_immediate_threshold:
        return FlagBand.immediate
    if score >= settings.risk_triage_threshold:
        return FlagBand.triage
    return FlagBand.none


def detect_concern_word(reflection: str | None, words: list[str]) -> tuple[list[str], float]:
    if not reflection:
        return [], 0.0
    text = reflection.lower()
    matched = [w for w in words if w in text]
    return matched, (CONCERN_WORD_SCORE if matched else 0.0)


def detect_slow_burn(db: Session, student_id: uuid.UUID) -> float:
    """Aggregate by DISTINCT DAY, not raw rows: a multi-day low streak flags even with a missed
    day; many check-ins on one day can't trip it; a recovered day clears it.
    """
    since = _utcnow() - timedelta(days=settings.slowburn_window_days)
    recent = checkin_db.get_checkins_since(db, student_id, since)
    if not recent:
        return 0.0
    days: dict[object, list[int]] = {}
    for c in recent:
        days.setdefault(c.submitted_at.date(), []).append(c.mood_value)
    low = settings.slowburn_low_mood_threshold
    all_days_low = all(all(m <= low for m in moods) for moods in days.values())
    if len(days) >= 2 and all_days_low:
        return SLOW_BURN_SCORE
    return 0.0


def score_checkin(db: Session, checkin: CheckIn) -> ScoreResult:
    """Score one check-in; create a Flag if warranted. NEVER acts on the student (surfaces only)."""
    words = resolve_words(db, checkin.school_id)
    matched, cw_score = detect_concern_word(checkin.reflection_text, words)
    sb_score = detect_slow_burn(db, checkin.student_id)

    checkin_db.mark_scored(db, checkin)
    if matched:
        flag_type, score = FlagType.concern_word, cw_score
    elif sb_score >= settings.risk_triage_threshold:
        flag_type, score = FlagType.slow_burn, sb_score
    else:
        return ScoreResult(
            flagged=False, risk_score=max(cw_score, sb_score), matched_terms=[], flag_id=None
        )

    flag = risk_db.create_flag(
        db,
        student_id=checkin.student_id,
        school_id=checkin.school_id,
        checkin_id=checkin.id,
        flag_type=flag_type,
        risk_score=score,
        band=route_band(score),
    )
    return ScoreResult(flagged=True, risk_score=score, matched_terms=matched, flag_id=flag.id)


def process_pending(db: Session) -> int:
    """Worker: score unscored check-ins. Per-item commit; a failed item is surfaced (CRITICAL) and
    dead-lettered so it is never reprocessed forever."""
    pending = checkin_db.get_unscored_for_update(db)
    processed = 0
    for c in pending:
        checkin_id = c.id
        try:
            score_checkin(db, c)
            db.commit()
            processed += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.critical("fr_12_01_error checkin=%s", checkin_id)
            fresh = checkin_db.get_checkin(db, checkin_id)
            if fresh is not None:
                checkin_db.mark_scored(db, fresh)  # dead-letter
                db.commit()
    return processed
