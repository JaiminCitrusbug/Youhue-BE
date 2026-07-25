"""Class listing schema — see `src.routers.classes` for why this minimal GET was added."""
import uuid

from pydantic import BaseModel


class ClassOut(BaseModel):
    id: uuid.UUID
    name: str


class MyClassesResponse(BaseModel):
    classes: list[ClassOut]
