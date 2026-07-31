"""Student read endpoint (INFRA-02/03) + parental-consent capture (FR-20-06) — both staff-only,
cross-school/class denied (403) + audited. Also the student's own check-in history (FR-08-01) —
self-only, `StudentDep`-scoped, no student_id parameter. Also the private supportive-note send +
reads (FR-13-05, SC-041) — a teacher's quiet note to their own/shared-class student, visible to no
one but the intended student and the sender (NEG gate). Thin router — business logic lives in
src.application.students.services / src.application.compliance.services /
src.application.checkin.services."""
import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from src.application.checkin import services as checkin_svc
from src.application.compliance import services as compliance_svc
from src.application.students import services as students_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep, StudentDep
from src.schemas.checkin import HistoryOut, MoodPointOut, ReflectionPointOut
from src.schemas.compliance import ConsentIn, ConsentOut
from src.schemas.students import (
    NoteCreate,
    NoteListItemOut,
    NoteListResponse,
    NoteOut,
    StudentDetailOut,
    StudentOut,
)

logger = logging.getLogger("youhue.checkin")
router = APIRouter(prefix="/students", tags=["students"])


@router.get(
    "/me/history",
    response_model=HistoryOut,
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Could not resolve the caller's own history.",
        },
    },
)
def get_my_history(student: StudentDep, db: DbDep) -> HistoryOut:
    """FR-08-01 (SC-025) — the CALLER's own moods over time + own reflections. Empty lists (never
    an error) before the caller has any check-ins (Scenario 3). `StudentDep` resolves the caller off
    the session — there is no student_id anywhere in this route, so a student can never reach
    another student's history (Scenario 2)."""
    try:
        moods, reflections = checkin_svc.get_student_history(db, student)
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception("fr_08_01_error action=get_my_history student_id=%s", student.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve your history"
        ) from exc
    logger.info(
        "fr_08_01_success action=get_my_history student_id=%s checkins=%s", student.id, len(moods)
    )
    return HistoryOut(
        moods_over_time=[MoodPointOut(local_date=d, mood_value=v) for d, v in moods],
        reflections=[ReflectionPointOut(local_date=d, reflection_text=t) for d, t in reflections],
    )


@router.get(
    "/me/notes",
    response_model=NoteListResponse,
    responses={
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "description": "Could not resolve the caller's own notes.",
        },
    },
)
def get_my_notes(student: StudentDep, db: DbDep) -> NoteListResponse:
    """FR-13-05 (SC-041) — the CALLER's own private supportive notes, newest first (ticket DoD:
    "delivered privately to that student"). Declared before `/{student_id}` (same reason as
    `/me/history` above): `StudentDep` resolves the caller off the session, so a student can never
    reach another student's notes."""
    try:
        notes = students_svc.list_my_notes(db, student)
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception("fr_13_05_error action=list_my_notes student_id=%s", student.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve your notes"
        ) from exc
    return NoteListResponse(
        notes=[NoteListItemOut(note_id=n.id, body=n.body, at=n.at) for n in notes]
    )


@router.get("/{student_id}", response_model=StudentOut)
def get_student(student_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StudentOut:
    s = students_svc.read_student(db, staff, student_id)
    return StudentOut(
        id=s.id, display_name=s.display_name, age_band=s.age_band.value, school_id=s.school_id
    )


@router.get(
    "/{student_id}/detail",
    response_model=StudentDetailOut,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Student is not in the caller's own/shared class scope.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such student."},
    },
)
def get_student_detail(student_id: uuid.UUID, staff: StaffDep, db: DbDep) -> StudentDetailOut:
    """FR-10-02 (SC-028) — a teacher's drill-in from the class dashboard into a single student:
    mood history, reflections and participation (rendered from the single owner, never
    recomputed)."""
    return students_svc.get_student_detail(db, staff, student_id)


@router.post("/{student_id}/consent", response_model=ConsentOut)
def capture_consent(
    student_id: uuid.UUID, body: ConsentIn, staff: StaffDep, db: DbDep
) -> ConsentOut:
    """FR-20-06: capture verifiable parental consent (SC-088, school-mediated, leadership-only).
    200 { status } on success; 422 on an invalid consent record (pydantic rejects an out-of-enum
    `status` before this body runs); 403 cross-school / wrong role (audited)."""
    try:
        recorded = compliance_svc.capture_consent(db, staff, student_id, body.status)
    except HTTPException:
        db.commit()  # persist the audit-logged denial / attempt before surfacing the error
        raise
    db.commit()
    return ConsentOut(status=recorded)


@router.post(
    "/{student_id}/notes",
    response_model=NoteOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Student is not in the caller's own/shared class scope.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such student."},
        status.HTTP_500_INTERNAL_SERVER_ERROR: {"description": "Could not send the note."},
    },
)
def send_note(student_id: uuid.UUID, body: NoteCreate, staff: StaffDep, db: DbDep) -> NoteOut:
    """FR-13-05 (SC-041) — a teacher sends a quiet, private, supportive note to their OWN/shared
    -class student (403 otherwise). 201 `{note_id}` — private to the target student, never public,
    never visible to any other staff member (NEG gate, the whole point of this ticket)."""
    try:
        note, _created = students_svc.send_private_note(db, staff, student_id, body.body)
    except HTTPException:
        db.commit()  # persist the audit-logged denial before surfacing the error
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        db.rollback()
        logger.exception("fr_13_05_error action=send_note actor_id=%s student_id=%s",
                          staff.id, student_id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not send the note"
        ) from exc
    db.commit()
    return NoteOut(note_id=note.id)


@router.get(
    "/{student_id}/notes",
    response_model=NoteListResponse,
    responses={
        status.HTTP_403_FORBIDDEN: {
            "description": "Student is not in the caller's own/shared class scope.",
        },
        status.HTTP_404_NOT_FOUND: {"description": "No such student."},
    },
)
def get_notes_sent_to_student(
    student_id: uuid.UUID, staff: StaffDep, db: DbDep
) -> NoteListResponse:
    """FR-13-05 — the caller's OWN sent notes to this student only (never a co-teacher's notes on
    the same student — the NEG gate this ticket exists to protect). Not a staff-facing roster/list
    surface: scoped to one student, filtered to one sender."""
    try:
        notes = students_svc.list_notes_sent_to_student(db, staff, student_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — last-resort guard, never leak an unhandled 500
        logger.exception(
            "fr_13_05_error action=list_sent_notes actor_id=%s student_id=%s",
            staff.id, student_id,
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not resolve sent notes"
        ) from exc
    return NoteListResponse(
        notes=[NoteListItemOut(note_id=n.id, body=n.body, at=n.at) for n in notes]
    )
