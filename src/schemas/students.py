"""Student request/response schemas."""
import uuid

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
