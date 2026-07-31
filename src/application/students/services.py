"""Student read business logic — school+class authorization + audit (INFRA-02/03). DB via domain."""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.application.calendar import services as calendar_svc
from src.application.checkin import services as checkin_svc
from src.application.derived import services as derived
from src.application.isolation import services as isolation
from src.domain.checkin import services as checkin_db
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount, Student
from src.domain.risk import services as risk_db
from src.domain.risk.models import SupportiveNote
from src.schemas.checkin import MoodPointOut, ReflectionPointOut
from src.schemas.students import StudentDetailOut

logger = logging.getLogger("youhue.students")

_DETAIL_PERIOD_KEY = "this_week"  # FR-10-02's own DoD carries no `range` param — same default
# period FR-10-01's dashboard uses (FR-10-03 is the ticket that adds real range filtering, and
# only to the CLASS dashboard endpoint; this student-detail endpoint stays independent, per the
# ticket's own DoD).


def _audit(db: Session, staff: StaffAccount, action: str, target: str) -> None:
    isolation.audit(db, actor_id=staff.id, action=action, target=target, school_id=staff.school_id)
    db.commit()


def read_student(db: Session, staff: StaffAccount, student_id: uuid.UUID) -> Student:
    # INFRA-02 (which-rows): a cross-school or unknown id resolves to None -> existence is hidden.
    student = isolation.get_scoped(db, Student, student_id, staff.school_id)
    if student is None:
        _audit(db, staff, "student.read.denied", str(student_id))
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    try:
        authz.require_student_access(db, staff, student)  # INFRA-03 (who-may): role + class scope
    except HTTPException:
        _audit(db, staff, "student.read.denied", str(student.id))
        raise
    _audit(db, staff, "student.read", str(student.id))
    return student


@derived.owns("student.participation_rate")
def compute_participation_rate(completed: int, expected_days: int) -> float:
    """The single-owner participation formula (ticket DoD, §13.5): check-ins completed divided by
    check-ins expected in the window. `expected_days` is the resolved period's calendar-day count
    (D-05/D-18-style interim, config-driven-if-needed formula — this codebase's established
    placeholder-pending-ratification posture for an unratified denominator definition; no school
    Mon-Fri/holiday calendar exists yet to refine it against). Guards a genuinely impossible
    zero-length window rather than dividing by zero."""
    if expected_days <= 0:
        return 0.0
    return round(completed / expected_days, 4)


def get_student_detail(db: Session, staff: StaffAccount, student_id: uuid.UUID) -> StudentDetailOut:
    """FR-10-02 (SC-028): a teacher's drill-in from the class dashboard into a single student.
    DIFFERENT error shape from `read_student` above (ticket's own explicit DoD, not the
    hide-existence pattern): a student that doesn't exist AT ALL is 404; a real student outside the
    caller's own/shared class scope (including a different school) is 403 — the caller learns
    "not found" only for a genuinely unknown id, never for one that exists but isn't theirs."""
    student = identity_db.get_student(db, student_id)
    if student is None:
        logger.info(
            "fr_10_02_rejected action=get_detail actor_id=%s reason=not_found student_id=%s",
            staff.id, student_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    if student.school_id != staff.school_id:
        _audit(db, staff, "student.detail.cross_tenant_denied", str(student.id))
        logger.warning(
            "fr_10_02_forbidden action=get_detail actor_id=%s reason=cross_tenant "
            "student_id=%s", staff.id, student_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    try:
        authz.require_student_access(db, staff, student)
    except HTTPException:
        _audit(db, staff, "student.detail.denied", str(student.id))
        logger.warning(
            "fr_10_02_forbidden action=get_detail actor_id=%s reason=out_of_scope "
            "student_id=%s", staff.id, student_id,
        )
        raise

    moods, reflections = checkin_svc.get_student_history(db, student)

    start, end = calendar_svc.resolve_period(db, staff, _DETAIL_PERIOD_KEY)
    in_window = checkin_db.list_checkins_for_students_between(db, [student.id], start, end)
    expected_days = (end - start).days
    participation = derived.compute(
        "student.participation_rate", len(in_window), expected_days
    )

    _audit(db, staff, "student.detail.read", str(student.id))
    logger.info(
        "fr_10_02_success action=get_detail actor_id=%s student_id=%s participation=%s",
        staff.id, student.id, participation,
    )
    return StudentDetailOut(
        mood_history=[MoodPointOut(local_date=d, mood_value=v) for d, v in moods],
        reflections=[ReflectionPointOut(local_date=d, reflection_text=t) for d, t in reflections],
        participation_rate=participation,
    )


# =================================================================================================
# FR-13-05 (SC-041) — POST /api/v1/students/{id}/notes: a teacher sends a quiet, private,
# supportive note to their OWN/shared-class student. NEG gate (the whole point of this ticket):
# visible to no one but the intended student and the sender — never public, never visible to peers
# or unrelated staff, not even a co-teacher who otherwise shares class access to the same student.
# Same three-tier error shape as `get_student_detail` above: unknown id -> 404, cross-tenant -> 403,
# out-of-scope teacher -> 403 (reuses `authz.require_student_access`, FR-10-02's own primitive).
# =================================================================================================


def _resolve_student_for_staff(db: Session, staff: StaffAccount, student_id: uuid.UUID) -> Student:
    student = identity_db.get_student(db, student_id)
    if student is None:
        logger.info(
            "fr_13_05_rejected action=send_note actor_id=%s reason=not_found student_id=%s",
            staff.id, student_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    if student.school_id != staff.school_id:
        _audit(db, staff, "student.note.cross_tenant_denied", str(student.id))
        logger.warning(
            "fr_13_05_forbidden action=send_note actor_id=%s reason=cross_tenant student_id=%s",
            staff.id, student_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    try:
        authz.require_student_access(db, staff, student)
    except HTTPException:
        _audit(db, staff, "student.note.denied", str(student.id))
        logger.warning(
            "fr_13_05_forbidden action=send_note actor_id=%s reason=out_of_scope student_id=%s",
            staff.id, student_id,
        )
        raise
    return student


def send_private_note(
    db: Session, staff: StaffAccount, student_id: uuid.UUID, body: str
) -> tuple[SupportiveNote, bool]:
    """Returns `(note, created)` — `created=False` on an exact-content retry within a short window
    (Baseline BR-05 idempotency; see `risk_db.find_recent_duplicate_note`'s own docstring for why
    exact-content-within-window, not a client key: the ticket's DoD payload is `{body: string}`
    only). Caller commits."""
    student = _resolve_student_for_staff(db, staff, student_id)

    duplicate = risk_db.find_recent_duplicate_note(
        db, student_id=student.id, sender_id=staff.id, body=body
    )
    if duplicate is not None:
        logger.info(
            "fr_13_05_success action=send_note actor_id=%s student_id=%s note_id=%s "
            "reason=idempotent_replay", staff.id, student.id, duplicate.id,
        )
        return duplicate, False

    note = risk_db.create_supportive_note(
        db, student_id=student.id, sender_id=staff.id, body=body
    )
    _audit(db, staff, "student.note.sent", str(student.id))
    logger.info(
        "fr_13_05_success action=send_note actor_id=%s student_id=%s note_id=%s",
        staff.id, student.id, note.id,
    )
    return note, True


def list_notes_sent_to_student(
    db: Session, staff: StaffAccount, student_id: uuid.UUID
) -> list[SupportiveNote]:
    """The SENDER's own read-back (ticket DoD: "the read path returns the note to the intended
    student and the sender only") — filtered to notes THIS staff member sent (never every note
    addressed to the student, even for a co-teacher who otherwise shares class access — the NEG
    gate this ticket exists to protect)."""
    student = _resolve_student_for_staff(db, staff, student_id)
    notes = risk_db.list_notes_for_student_and_sender(db, student.id, staff.id)
    logger.info(
        "fr_13_05_success action=list_sent_notes actor_id=%s student_id=%s count=%s",
        staff.id, student.id, len(notes),
    )
    return notes


def list_my_notes(db: Session, student: Student) -> list[SupportiveNote]:
    """The intended STUDENT's own read path — self-only, `StudentDep`-scoped (no student_id
    anywhere in the route), the same structural self-scoping FR-08-01's `/me/history` uses: a
    student can never reach another student's notes, structurally, not merely by a runtime check."""
    notes = risk_db.list_notes_for_student(db, student.id)
    logger.info(
        "fr_13_05_success action=list_my_notes student_id=%s count=%s", student.id, len(notes)
    )
    return notes
