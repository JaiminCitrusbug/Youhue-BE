"""Risk domain services — DB access only. M-12 is the sole writer of Flag."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import FlagBand, FlagStatus, FlagType
from src.domain.risk.models import ConcernWordList, Flag


def get_concern_word_list(db: Session, school_id: uuid.UUID) -> ConcernWordList | None:
    return db.scalar(select(ConcernWordList).where(ConcernWordList.school_id == school_id))


def create_flag(
    db: Session,
    *,
    student_id: uuid.UUID,
    school_id: uuid.UUID,
    checkin_id: uuid.UUID | None,
    flag_type: FlagType,
    risk_score: float,
    band: FlagBand,
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
