"""Roster CSV import (FR-03-01) response schema.

The request body is a multipart file upload (``UploadFile``), not JSON — there is no request model
here on purpose. Error bodies reuse the ``{code, message}`` shape already established by
``src.utils.file_scan`` / ``src.application.roster.services`` (415/413/400) and the row-level
``errors`` list shape for 422, so the frontend has one error contract, not several.
"""
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
