"""Risk domain services — DB access only. M-12 is the sole writer of Flag."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import FlagBand, FlagStatus, FlagType
from src.domain.risk.models import ConcernWordList, Flag


def get_concern_word_list(db: Session, school_id: uuid.UUID) -> ConcernWordList | None:
    """A school's OVERRIDE list, if any (never the platform default — that has a NULL school_id and
    is_default true). Filtering on is_default false keeps override and default lookups disjoint."""
    return db.scalar(
        select(ConcernWordList).where(
            ConcernWordList.school_id == school_id,
            ConcernWordList.is_default.is_(False),
        )
    )


def get_default_concern_word_list(db: Session) -> ConcernWordList | None:
    """The single platform DEFAULT list (FR-19-05) — school_id NULL, is_default true; None until
    the internal team has seeded one. Consumed by INFRA-06 when a school has no override."""
    return db.scalar(select(ConcernWordList).where(ConcernWordList.is_default.is_(True)))


def set_default_concern_word_list(db: Session, words: list[str]) -> ConcernWordList:
    """Upsert the platform default list (FR-19-05). Idempotent on retry and NEVER touches a school
    override row (GATE G-6) — it reads/writes only the is_default=true record. Caller commits."""
    row = get_default_concern_word_list(db)
    if row is None:
        row = ConcernWordList(school_id=None, words=list(words), is_default=True)
        db.add(row)
    else:
        row.words = list(words)
    db.flush()
    return row


def get_flag_by_checkin(db: Session, checkin_id: uuid.UUID) -> Flag | None:
    return db.scalar(select(Flag).where(Flag.checkin_id == checkin_id))


def create_flag(
    db: Session,
    *,
    student_id: uuid.UUID,
    school_id: uuid.UUID,
    checkin_id: uuid.UUID | None,
    flag_type: FlagType,
    risk_score: float,
    band: FlagBand | None = None,  # unrouted at creation — FR-12-06 sets the action band
) -> Flag:
    flag = Flag(
        student_id=student_id,
        school_id=school_id,
        checkin_id=checkin_id,
        type=flag_type,
        risk_score=risk_score,
        band=band,
        status=FlagStatus.open,
    )
    db.add(flag)
    db.flush()
    return flag
