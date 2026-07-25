"""Risk domain services — DB access only. M-12 is the sole writer of Flag."""
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import FlagBand, FlagStatus, FlagType
from src.domain.risk.models import AlertRecipientConfig, ConcernWordList, Flag


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


def set_school_concern_word_list(
    db: Session, school_id: uuid.UUID, words: list[str]
) -> ConcernWordList:
    """Upsert a school's OVERRIDE list (FR-16-02, leadership-owned). Idempotent on retry and NEVER
    touches the platform default row (GATE G-6) — filtered strictly on ``school_id`` + ``is_default
    false``, the same disjoint keying ``get_concern_word_list`` reads with. Caller commits."""
    row = get_concern_word_list(db, school_id)
    if row is None:
        row = ConcernWordList(school_id=school_id, words=list(words), is_default=False)
        db.add(row)
    else:
        row.words = list(words)
    db.flush()
    return row


def get_alert_recipient_configs(db: Session, school_id: uuid.UUID) -> list[AlertRecipientConfig]:
    """Every alert-routing row for a school (FR-16-02 read), one per ``alert_type``."""
    return list(
        db.scalars(
            select(AlertRecipientConfig)
            .where(AlertRecipientConfig.school_id == school_id)
            .order_by(AlertRecipientConfig.alert_type)
        )
    )


def get_alert_recipient_config(
    db: Session, school_id: uuid.UUID, alert_type: str
) -> AlertRecipientConfig | None:
    return db.scalar(
        select(AlertRecipientConfig).where(
            AlertRecipientConfig.school_id == school_id,
            AlertRecipientConfig.alert_type == alert_type,
        )
    )


def set_alert_recipient_config(
    db: Session, school_id: uuid.UUID, alert_type: str, recipient_staff_ids: list[uuid.UUID]
) -> AlertRecipientConfig:
    """Upsert the ordered recipient chain for one alert_type (FR-16-02 stores ONLY the config; the
    escalation/order ENGINE that consumes it is FR-12-05 — out of this ticket's scope). Order is
    carried by array position in ``recipient_staff_ids``, not ``order_index`` (reserved for a future
    multi-rule-per-alert-type extension FR-12-05 may need; unused here). Caller commits.

    KNOWN GAP (logged, not silently reconciled): there is no DB-level unique constraint on
    (school_id, alert_type) — this read-then-write is an application-level upsert, not a DB-enforced
    one. Acceptable for this ticket's low-concurrency leadership-config-edit surface; a real unique
    index is a schema change left for FR-12-05 if it needs a stronger guarantee."""
    row = get_alert_recipient_config(db, school_id, alert_type)
    if row is None:
        row = AlertRecipientConfig(
            school_id=school_id,
            alert_type=alert_type,
            recipient_staff_ids=list(recipient_staff_ids),
            order_index=0,
        )
        db.add(row)
    else:
        row.recipient_staff_ids = list(recipient_staff_ids)
    db.flush()
    return row


def get_flag_by_checkin(db: Session, checkin_id: uuid.UUID) -> Flag | None:
    return db.scalar(select(Flag).where(Flag.checkin_id == checkin_id))


def get_open_flag(db: Session, student_id: uuid.UUID, flag_type: FlagType) -> Flag | None:
    """The student's own OPEN flag of this type, if any (FR-12-03 idempotency: a background
    slow-burn evaluation while one is already open must not raise a second one)."""
    return db.scalar(
        select(Flag).where(
            Flag.student_id == student_id,
            Flag.type == flag_type,
            Flag.status == FlagStatus.open,
        )
    )


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
