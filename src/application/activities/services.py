"""FR-14-02 — a teacher runs a seed-set activity with their whole class or assigns it to a specific
student, drawn from the internally maintained seed activity set (FR-19-04, already merged). Target
must be the teacher's OWN class — `class:{id}` (run with the whole class) or `student:{id}` (assign
to one student) — a cross-class or cross-tenant target is denied (403).

Reuses, does not reinvent: `authz.require_class_access`/`require_student_access` (INFRA-03, the SAME
owner+shared class-scope model FR-10-01/FR-10-02 already use — the ticket's "own class" is read as
this codebase's established scope, not a stricter owner-only carve-out) and
`checkin_db.create_activity_engagement` (FR-05-01's own writer). `checkin_id=None` on every row
written here — this is a teacher-initiated assignment, not tied to any particular check-in, unlike
FR-05-01's post-check-in offer.

Structured logs: `fr_14_02_success` / `_rejected` / `_forbidden` / `_error`.
"""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.domain.checkin import services as checkin_db
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount
from src.domain.org import services as org_db

logger = logging.getLogger("youhue.activities")


def _parse_target(target: str) -> tuple[str, uuid.UUID]:
    kind, sep, raw_id = target.partition(":")
    if not sep or kind not in ("class", "student"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid target")
    try:
        return kind, uuid.UUID(raw_id)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid target") from exc


def _run_with_class(db: Session, staff: StaffAccount, class_id: uuid.UUID) -> list[uuid.UUID]:
    klass = org_db.get_class(db, class_id)
    if klass is None:
        logger.info(
            "fr_14_02_rejected action=run_or_assign actor_id=%s reason=class_not_found "
            "class_id=%s", staff.id, class_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Class not found")
    try:
        authz.require_class_access(db, staff, class_id)
    except HTTPException:
        logger.warning(
            "fr_14_02_forbidden action=run_or_assign actor_id=%s reason=out_of_scope "
            "class_id=%s", staff.id, class_id,
        )
        raise
    return [s.id for s in org_db.list_students_in_class(db, class_id)]


def _assign_to_student(db: Session, staff: StaffAccount, student_id: uuid.UUID) -> list[uuid.UUID]:
    student = identity_db.get_student(db, student_id)
    if student is None:
        logger.info(
            "fr_14_02_rejected action=run_or_assign actor_id=%s reason=student_not_found "
            "student_id=%s", staff.id, student_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Student not found")
    try:
        authz.require_student_access(db, staff, student)
    except HTTPException:
        logger.warning(
            "fr_14_02_forbidden action=run_or_assign actor_id=%s reason=out_of_scope "
            "student_id=%s", staff.id, student_id,
        )
        raise
    return [student.id]


def run_or_assign_activity(
    db: Session, staff: StaffAccount, activity_id: uuid.UUID, target: str
) -> list[uuid.UUID]:
    """Returns the student ids the activity was assigned to (one for a `student:` target, every
    roster member for a `class:` target — a class with zero members is a valid empty result, not an
    error). Writes one `offered` `ActivityEngagement` per student."""
    activity = checkin_db.get_seed_activity(db, activity_id)
    if activity is None:
        logger.info(
            "fr_14_02_rejected action=run_or_assign actor_id=%s reason=activity_not_found "
            "activity_id=%s", staff.id, activity_id,
        )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Activity not found")

    kind, target_id = _parse_target(target)
    student_ids = (
        _run_with_class(db, staff, target_id)
        if kind == "class"
        else _assign_to_student(db, staff, target_id)
    )

    for student_id in student_ids:
        checkin_db.create_activity_engagement(
            db, student_id=student_id, activity_id=activity.id, checkin_id=None
        )
    logger.info(
        "fr_14_02_success action=run_or_assign actor_id=%s activity_id=%s target=%s assigned=%d",
        staff.id, activity_id, target, len(student_ids),
    )
    return student_ids
