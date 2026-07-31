"""Student request/response schemas."""
import uuid
from datetime import datetime

from pydantic import BaseModel

from src.schemas.checkin import MoodPointOut, ReflectionPointOut


class StudentOut(BaseModel):
    id: uuid.UUID
    display_name: str
    age_band: str
    school_id: uuid.UUID


class StudentDetailOut(BaseModel):
    """FR-10-02 (SC-028) — a teacher's drill-in view of a single student: mood history,
    reflections and participation. `participation_rate` is rendered from its single owner
    (`derived.compute("student.participation_rate", ...)`, `application.students.services`),
    never recomputed by the FE [Baseline BR-05]."""

    mood_history: list[MoodPointOut]
    reflections: list[ReflectionPointOut]
    participation_rate: float


class NoteCreate(BaseModel):
    """FR-13-05 (SC-041) — POST /api/v1/students/{id}/notes body. Ticket's own exact DoD payload:
    `body: string` only (no flag_id, no visibility flag — the note is always private, never a
    caller-set choice)."""

    body: str


class NoteOut(BaseModel):
    """FR-13-05 — POST response, ticket's own exact DoD shape: `201 { note_id }`."""

    note_id: uuid.UUID


class NoteListItemOut(BaseModel):
    note_id: uuid.UUID
    body: str
    at: datetime


class NoteListResponse(BaseModel):
    notes: list[NoteListItemOut]
