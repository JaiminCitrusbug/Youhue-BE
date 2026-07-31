"""Risk domain services — DB access only. M-12 is the sole writer of Flag."""
import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.constants.enums import FlagBand, FlagEventType, FlagStatus, FlagType
from src.domain.risk.models import AlertRecipientConfig, ConcernWordList, Flag, FlagEvent


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
    """Upsert the ordered recipient chain for one alert_type (shared by FR-16-02's settings surface
    and FR-12-05's dedicated `PUT /alert-config` endpoint; the escalation/order ENGINE that consumes
    this config is FR-12-08 — out of both tickets' scope). Order is carried by array position in
    ``recipient_staff_ids``, not ``order_index`` (reserved for a future multi-rule-per-alert-type
    extension; unused here — a single row's order_index cannot represent a per-recipient order for
    a list-valued column, only a future per-rule ranking). Caller commits.

    FR-12-05 closed a KNOWN GAP this function used to have (a plain read-then-write with no DB
    backstop — see migration f7a3c9e21b04): now a real `INSERT ... ON CONFLICT (school_id,
    alert_type) DO UPDATE`, backed by `uq_alert_recipient_configs_school_type`. Two concurrent
    writers for the same (school_id, alert_type) converge on whichever commits last — a plain "set
    the config" semantic, not upsert-and-converge-onto-the-first-writer (unlike FR-12-03's
    open-flag upsert) or reject-the-loser (unlike FR-02-01's checkin upsert): a leadership config
    write has no "first writer wins" business meaning, the latest save is simply the config."""
    stmt = (
        pg_insert(AlertRecipientConfig)
        .values(
            school_id=school_id,
            alert_type=alert_type,
            recipient_staff_ids=list(recipient_staff_ids),
            order_index=0,
        )
        .on_conflict_do_update(
            index_elements=["school_id", "alert_type"],
            set_={"recipient_staff_ids": list(recipient_staff_ids)},
        )
    )
    db.execute(stmt)
    db.flush()
    row = get_alert_recipient_config(db, school_id, alert_type)
    if row is None:  # pragma: no cover - the upsert above guarantees a row exists by now
        raise RuntimeError(
            f"alert-config upsert invariant violated for school_id={school_id} "
            f"alert_type={alert_type}"
        )
    return row


def get_flag_by_checkin(db: Session, checkin_id: uuid.UUID) -> Flag | None:
    return db.scalar(select(Flag).where(Flag.checkin_id == checkin_id))


def get_open_flag(db: Session, student_id: uuid.UUID, flag_type: FlagType) -> Flag | None:
    """The student's own OPEN flag of this type, if any — a cheap read-only fast path (skip
    rescoring when the caller already knows a flag exists). NOT the idempotency mechanism itself
    (see `upsert_open_flag` for the DB-level backstop; a plain SELECT here does not prevent two
    concurrent callers from both observing None)."""
    return db.scalar(
        select(Flag).where(
            Flag.student_id == student_id,
            Flag.type == flag_type,
            Flag.status == FlagStatus.open,
        )
    )


def upsert_open_flag(
    db: Session,
    *,
    student_id: uuid.UUID,
    school_id: uuid.UUID,
    flag_type: FlagType,
    risk_score: float,
) -> Flag:
    """Race-safe create for a checkin_id=NULL (background-evaluation) flag: at most one OPEN flag
    per (student_id, type), backed by the partial unique index `uq_flags_open_student_type`
    (migration d1f6a3c8e952) — `uq_flags_checkin_id` gives no protection here since every row this
    inserts has `checkin_id=NULL`, and Postgres treats every NULL as distinct under a standard
    UNIQUE constraint (review Blocker, proven by execution: 2 duplicate open slow_burn flags on the
    first of 8 concurrent-evaluate iterations without this).

    `INSERT ... ON CONFLICT (student_id, type) WHERE status='open' DO NOTHING`, then
    `SELECT ... FOR UPDATE` returns the single open row EITHER WAY (ours, or the concurrent
    winner's, already locked) — the same upsert+lock shape as FR-19-02's
    `get_or_create_subscription` (upsert-and-converge, not FR-02-01's reject-the-loser: a
    re-evaluation racing with itself should converge onto the winner's flag, not error)."""
    db.execute(
        pg_insert(Flag)
        .values(
            student_id=student_id,
            school_id=school_id,
            checkin_id=None,
            type=flag_type,
            risk_score=risk_score,
            status=FlagStatus.open,
        )
        .on_conflict_do_nothing(
            index_elements=["student_id", "type"],
            index_where=text("status = 'open'"),
        )
    )
    flag = db.scalar(
        select(Flag)
        .where(
            Flag.student_id == student_id,
            Flag.type == flag_type,
            Flag.status == FlagStatus.open,
        )
        .with_for_update()
    )
    if flag is None:  # pragma: no cover - the upsert above guarantees a row exists by now
        raise RuntimeError(
            f"open-flag upsert invariant violated for student_id={student_id} type={flag_type}"
        )
    return flag


def get_flag(db: Session, flag_id: uuid.UUID) -> Flag | None:
    return db.get(Flag, flag_id)


def set_flag_band(db: Session, flag: Flag, band: FlagBand) -> Flag:
    """FR-12-06: persist the routing decision. `flag.band` is null until routed (see `create_flag`
    docstring); the routing service is the single owner of this write (render-only downstream, per
    the ticket's own out-of-scope line) — no other caller ever sets `Flag.band`."""
    flag.band = band
    db.flush()
    return flag


def record_flag_alerted(db: Session, flag_id: uuid.UUID) -> FlagEvent:
    """Immutable event marking that an immediate-band flag's configured adults were alerted
    (FR-12-04 dispatch; INFRA-05/FR-18-03 own actual delivery)."""
    event = FlagEvent(flag_id=flag_id, event_type=FlagEventType.alerted, actor_id=None)
    db.add(event)
    db.flush()
    return event


def has_flag_event(db: Session, flag_id: uuid.UUID, event_type: FlagEventType) -> bool:
    """FR-12-04 (BR-05): has this flag already recorded an event of this type? The idempotency
    check `dispatch_alert` uses before enqueueing — a retry of an already-alerted flag must not
    double-alert. Read-only; same query-based idempotency posture as `get_open_flag`'s fast path
    (good enough for retry-safety, not a true-concurrency backstop)."""
    return (
        db.scalar(
            select(FlagEvent).where(
                FlagEvent.flag_id == flag_id, FlagEvent.event_type == event_type
            )
        )
        is not None
    )


def list_flag_events(db: Session, flag_id: uuid.UUID) -> list[FlagEvent]:
    """FR-12-09: a flag's full immutable timeline, chronological (oldest first) — alerted
    (FR-12-04), viewed/acted (FR-13-04/05), escalated (FR-12-08). Read-only; this module writes no
    new event type — `record_flag_alerted` above remains the sole writer this ticket touches."""
    return list(
        db.scalars(select(FlagEvent).where(FlagEvent.flag_id == flag_id).order_by(FlagEvent.at))
    )


def list_open_triage_flags(db: Session, school_id: uuid.UUID) -> list[Flag]:
    """SC-038 triage queue: open, triage-band flags for a school, oldest first (human review
    order) — read-only, render-only surface (GATE G-9: no automated action follows from a read)."""
    return list(
        db.scalars(
            select(Flag)
            .where(
                Flag.school_id == school_id,
                Flag.band == FlagBand.triage,
                Flag.status == FlagStatus.open,
            )
            .order_by(Flag.created_at)
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
