"""Student request/response schemas."""
import uuid

from pydantic import BaseModel


class StudentOut(BaseModel):
    id: uuid.UUID
    display_name: str
    age_band: str
    school_id: uuid.UUID
