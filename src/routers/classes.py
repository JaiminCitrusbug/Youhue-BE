"""Class listing (FR-02-03 delta addition): InviteColleague.tsx (SC-059) needs the caller's OWNED
classes to populate its 'Shared class' picker with real data, never a fixture — no prior ticket
exposed a class-list read endpoint. Mirrors the established minimal-GET-add precedent (FR-02-02's
two read endpoints, FR-03-05's ``GET /schools/{id}/roster``): not in the ticket's literal "What to
build", added because rendering the approved screen honestly requires something real to read.
"""
from fastapi import APIRouter

from src.application.classes import services as classes_svc
from src.infrastructure.middlewares.auth_middleware import DbDep, StaffDep
from src.schemas.classes import MyClassesResponse

router = APIRouter(prefix="/classes", tags=["classes"])


@router.get("/mine", response_model=MyClassesResponse)
def get_my_classes(staff: StaffDep, db: DbDep) -> MyClassesResponse:
    """Read-only — no transaction to commit/roll back."""
    return classes_svc.list_owned_classes(db, staff)
