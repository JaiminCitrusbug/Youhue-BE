"""Risk-scoring pipeline (INFRA-06). Scores each check-in (async, via the CheckIn.scored queue flag)
and produces a risk_score (0.00-1.00) + Flag. Pluggable detectors: concern-word (FR-12-01) and
slow-burn (FR-12-03). It SURFACES signal — it never takes any automated action on a student (a
human acts). Thresholds + the default concern-word list are env-driven (D-05 pending). Scoring
biases toward flagging over missing; a scoring error is surfaced (CRITICAL), never silently dropped.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.application import derived
from src.config import settings
from src.domain.enums import FlagBand, FlagStatus, FlagType
from src.infrastructure.models.checkin import CheckIn
from src.infrastructure.models.risk import ConcernWordList, Flag

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
    row = db.scalar(select(ConcernWordList).where(ConcernWordList.school_id == school_id))
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
    recent = list(
        db.scalars(
            select(CheckIn).where(CheckIn.student_id == student_id, CheckIn.submitted_at >= since)
        )
    )
    if not recent:
        return 0.0
    days: dict[object, list[int]] = {}
    for c in recent:
        days.setdefault(c.submitted_at.date(), []).append(c.mood_value)
    low = settings.slowburn_low_mood_threshold
    all_days_low = all(all(m <= low for m in moods) for moods in days.values())
    # >= 2 distinct days (a real multi-day pattern) and every day stayed low
    if len(days) >= 2 and all_days_low:
        return SLOW_BURN_SCORE
    return 0.0


def score_checkin(db: Session, checkin: CheckIn) -> ScoreResult:
    """Score one check-in; create a Flag if warranted. NEVER acts on the student (surfaces only)."""
    words = resolve_words(db, checkin.school_id)
    matched, cw_score = detect_concern_word(checkin.reflection_text, words)
    sb_score = detect_slow_burn(db, checkin.student_id)

    checkin.scored = True
    if matched:
        flag_type, score = FlagType.concern_word, cw_score
    elif sb_score >= settings.risk_triage_threshold:
        flag_type, score = FlagType.slow_burn, sb_score
    else:
        db.flush()
        return ScoreResult(
            flagged=False, risk_score=max(cw_score, sb_score), matched_terms=[], flag_id=None
        )

    flag = Flag(
        student_id=checkin.student_id,
        school_id=checkin.school_id,
        checkin_id=checkin.id,
        type=flag_type,
        risk_score=score,
        band=route_band(score),
        status=FlagStatus.open,
    )
    db.add(flag)
    db.flush()
    return ScoreResult(flagged=True, risk_score=score, matched_terms=matched, flag_id=flag.id)


def process_pending(db: Session) -> int:
    """Worker: score unscored check-ins (durable queue). Per-item commit so one failure can't poison
    the batch; a failed item is surfaced (CRITICAL) and dead-lettered (scored=True) so it is never
    reprocessed forever. `SKIP LOCKED` prevents concurrent workers double-scoring the same row.
    """
    pending = list(
        db.scalars(
            select(CheckIn).where(CheckIn.scored.is_(False)).with_for_update(skip_locked=True)
        )
    )
    processed = 0
    for c in pending:
        checkin_id = c.id
        try:
            score_checkin(db, c)
            db.commit()
            processed += 1
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.critical("fr_12_01_error checkin=%s", checkin_id)  # surfaced, not dropped
            fresh = db.get(CheckIn, checkin_id)
            if fresh is not None:
                fresh.scored = True  # dead-letter — do not loop on it forever
                db.commit()
    return processed
