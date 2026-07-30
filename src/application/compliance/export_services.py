"""School data export (FR-20-01, GATE G-12). Own module — a genuinely separate concern from
`compliance/services.py`'s consent gate, sharing only the `compliance` domain package.

Async contract (ticket DoD): `POST /schools/{id}/export` -> 202 accepted (creates the DataExport
row, status=pending) -> the artifact is built and written to object storage AFTER the response is
sent (FastAPI `BackgroundTasks` — no new task-queue infra, a native, zero-dependency facility that
fits this ticket's explicit two-phase 202-then-poll contract) -> a follow-up
`GET /schools/{id}/exports/{export_id}` returns 200 `{status: "ready", ...}` once done.

Every export is scoped to the REQUESTING school only — the query layer below never accepts a
school_id other than the authorised caller's own (BR-01, FR-20-07 isolation contract).
"""
import json
import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config import db_connection
from src.application.authz import services as authz
from src.constants.enums import DataExportKind, StaffRole
from src.domain.checkin import services as checkin_db
from src.domain.compliance import services as compliance_db
from src.domain.compliance.models import DataExport
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount
from src.infrastructure import storage

logger = logging.getLogger("youhue.compliance.export")


def request_export(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, kind: DataExportKind
) -> DataExport:
    """GATE G-12: only authorised (leadership) staff at their OWN school may request an export —
    the role check runs BEFORE the school-scope check, so a non-leadership actor is denied
    regardless of which school they target (never leaks whether a school id is valid to them)."""
    try:
        authz.require_roles(actor, StaffRole.leadership)
    except HTTPException:
        logger.warning(
            "fr_20_01_forbidden action=request_export actor_id=%s reason=role", actor.id
        )
        raise
    if actor.school_id != school_id:
        logger.warning(
            "fr_20_01_forbidden action=request_export actor_id=%s reason=cross_tenant "
            "school_id=%s", actor.id, school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    export = compliance_db.create_export(
        db, school_id=school_id, requested_by=actor.id, kind=kind
    )
    logger.info(
        "fr_20_01_success action=request_export actor_id=%s school_id=%s export_id=%s",
        actor.id, school_id, export.id,
    )
    return export


def get_export_status(
    db: Session, actor: StaffAccount, school_id: uuid.UUID, export_id: uuid.UUID
) -> DataExport:
    try:
        authz.require_roles(actor, StaffRole.leadership)
    except HTTPException:
        logger.warning(
            "fr_20_01_forbidden action=get_export_status actor_id=%s reason=role", actor.id
        )
        raise
    if actor.school_id != school_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    export = compliance_db.get_export(db, export_id)
    if export is None or export.school_id != school_id:
        # a cross-tenant id reads as "not found" (existence hidden) — same as isolation.get_scoped
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Export not found")
    return export


def _build_school_export_payload(db: Session, school_id: uuid.UUID) -> bytes:
    """The school's own data ONLY — students, staff, check-ins, scoped by `school_id` at the query
    layer (never a cross-tenant row). Phase-1 scope: no per-child clinical/flag detail included here
    (that stays inside the platform, consistent with FR-19-07's "no per-child data" isolation
    posture) — the school keeps control of its OWN roster/check-in records, this ticket's DoD."""
    students = identity_db.list_students_in_school(db, school_id)
    staff = identity_db.list_staff_in_school(db, school_id)
    checkins = checkin_db.list_checkins_for_school(db, school_id)
    payload = {
        "school_id": str(school_id),
        "exported_at": datetime.now(UTC).isoformat(),
        "students": [
            {"id": str(s.id), "display_name": s.display_name, "age_band": s.age_band.value}
            for s in students
        ],
        "staff": [
            {"id": str(s.id), "email": s.email, "role": s.role.value, "status": s.status.value}
            for s in staff
        ],
        "check_ins": [
            {
                "id": str(c.id), "student_id": str(c.student_id), "mood_value": c.mood_value,
                "reflection_text": c.reflection_text, "submitted_at": c.submitted_at.isoformat(),
            }
            for c in checkins
        ],
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def build_and_store_export(export_id: uuid.UUID) -> None:
    """The BackgroundTasks entrypoint — runs AFTER the 202 response is sent, in its own session
    (the request's session is closed by then). A failure here leaves the row `pending` forever
    rather than silently vanishing — surfaced via logging (CRITICAL); a stuck `pending` row is
    visible to a status poll, never a false `ready`.

    Reads `db_connection.SessionLocal` via module attribute (not a frozen import) so tests can
    monkeypatch it to the isolated test engine — this background task can't go through the usual
    `get_db`/`app.dependency_overrides` FastAPI DI path (the request that would carry that override
    has already finished), so it needs its own escape hatch (see
    `tests/test_data_export.py::_background_task_uses_test_db`)."""
    db = db_connection.SessionLocal()
    try:
        export = compliance_db.get_export(db, export_id)
        if export is None:  # pragma: no cover - defensive; the row was just created by the caller
            return
        try:
            body = _build_school_export_payload(db, export.school_id)
            key = f"exports/{export.school_id}/{export.id}.json"
            storage.put_object(key, body)
            compliance_db.mark_export_ready(db, export, key)
            db.commit()
            logger.info(
                "fr_20_01_success action=build_and_store_export export_id=%s key=%s",
                export_id, key,
            )
        except Exception:  # noqa: BLE001 - never let a background failure vanish silently
            db.rollback()
            logger.critical(
                "fr_20_01_error action=build_and_store_export export_id=%s", export_id
            )
    finally:
        db.close()
