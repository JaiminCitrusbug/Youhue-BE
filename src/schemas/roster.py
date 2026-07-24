"""Roster CSV import (FR-03-01) + re-import reconciliation (FR-03-05) + roster list (SC-037)
response schemas.

The request body for import/reconcile is a multipart file upload (``UploadFile``), not JSON —
there is no request model here on purpose. Error bodies reuse the ``{code, message}`` shape already
established by ``src.utils.file_scan`` / ``src.application.roster.services`` (415/413/400) and the
row-level ``errors`` list shape for 422, so the frontend has one error contract, not several — the
reconcile endpoint reuses the SAME validation pipeline and therefore the SAME error shapes.
"""
import uuid
from typing import Literal

from pydantic import BaseModel


class RosterImportResponse(BaseModel):
    """200 body — Definition of done: `{ imported, banded }`.

    ``imported`` counts students newly CREATED this call; ``banded`` counts every row that was
    successfully age-banded (create OR update) — on a pure re-import of an unchanged roster,
    ``imported`` can be 0 while ``banded`` reflects every row that was processed.
    """

    imported: int
    banded: int


class RosterErrorDetail(BaseModel):
    """Machine-readable reason shared by every rejection this endpoint can return except the
    row-level 422 (``RosterRowErrorDetail`` below). ``code`` is the field to branch on; ``message``
    names the TRUE reason (ticket §Interaction contract) and is display copy."""

    code: Literal["unsupported_file_type", "file_too_large", "failed_scan"]
    message: str


class RosterErrorResponse(BaseModel):
    """415 / 413 / 400 body."""

    detail: RosterErrorDetail


class RosterRowError(BaseModel):
    row: int
    error: str


class RosterRowErrorDetail(BaseModel):
    code: Literal["malformed_rows"]
    message: str
    errors: list[RosterRowError]


class RosterRowErrorResponse(BaseModel):
    """422 body — every malformed row listed in one response, not one-at-a-time."""

    detail: RosterRowErrorDetail


class RosterReconcileResponse(BaseModel):
    """200 body for FR-03-05 ``POST /roster/reconcile`` — Definition of done: real counts, never a
    fixture number.

    ``stayers_active``: rows matched (by ``external_ref``) to an already-active student -> stays
    active, untouched otherwise.
    ``leavers_deactivated``: previously-active students whose ``external_ref`` is absent from this
    upload -> marked inactive; the row itself is never deleted.
    ``new_added``: rows with no matching existing student (new external_ref, no external_ref at
    all, or a match against a previously-inactive student who is reactivated as returning) ->
    created/reactivated as active.
    """

    stayers_active: int
    leavers_deactivated: int
    new_added: int


class RosterStudentOut(BaseModel):
    """One row of the FR-03-05 SC-037 roster list (GET /roster) — real DB state, not a fixture."""

    id: uuid.UUID
    display_name: str
    age_band: str
    status: str
    external_ref: str | None


class RosterListResponse(BaseModel):
    """200 body for ``GET /schools/{id}/roster`` — every student at the school, any status."""

    students: list[RosterStudentOut]
