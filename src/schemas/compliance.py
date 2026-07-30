"""Parental-consent request/response schemas (FR-20-06). Minimum-data posture: no surplus fields —
only what the consent record needs (status), never parent PII (name/email/phone) the FE doesn't
collect either (SC-088 is school-mediated attestation, not a parent-facing form)."""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from src.constants.enums import DataExportKind, DataExportStatus, ParentalConsentStatus


class ConsentIn(BaseModel):
    status: ParentalConsentStatus


class ConsentOut(BaseModel):
    status: ParentalConsentStatus


# ---- FR-20-01 — school data export ---------------------------------------------------------------


class ExportRequest(BaseModel):
    """POST /schools/{id}/export body. `kind` defaults to a plain export; `export_and_delete` is
    reserved for FR-20-02 (export-then-hard-delete on school exit), out of this ticket's scope."""

    kind: DataExportKind = DataExportKind.export


class ExportAccepted(BaseModel):
    export_id: uuid.UUID
    status: DataExportStatus


class ExportStatusOut(BaseModel):
    export_id: uuid.UUID
    kind: DataExportKind
    status: DataExportStatus
    created_at: datetime
    download_url: str | None = None  # set only once status=ready (a short-lived presigned URL)


# ---- FR-20-02 — school exit: export-then-hard-delete (SC-065) -----------------------------------


class ExportAndDeleteOut(BaseModel):
    """200 body for POST /schools/{id}/export-and-delete. The SAME endpoint answers both steps of
    the ordered exit: the first call offers the export (``deleted=False``, ``status="pending"``); a
    later call, once that export is ``ready``, performs the hard delete
    (``deleted=True``, ``status="completed"``) — after which the school no longer exists."""

    export_id: uuid.UUID
    status: DataExportStatus
    deleted: bool


class ExportAndDeleteConflictDetail(BaseModel):
    code: Literal["export_not_retrieved"]
    message: str


class ExportAndDeleteConflictResponse(BaseModel):
    """409 body — the ordering guard: a delete attempted before the export is ready/retrieved."""

    detail: ExportAndDeleteConflictDetail
