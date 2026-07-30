"""School exit: export-then-hard-delete (FR-20-02, SC-065). Reuses FR-20-01's export machinery in
full — the SAME `DataExport` row shape, background-build task and object-storage path, just
`kind=export_and_delete` — and adds the ordered, irreversible second step: once (and only once)
that export is `ready`, a further call hard-deletes the school's data
(`src.domain.compliance.services.hard_delete_school_cascade`).

Two-call, stateful contract on ONE endpoint (`POST /schools/{id}/export-and-delete`, both calls
return 200 per the ticket DoD — a deliberate divergence from FR-20-01's 202, this ticket's own
choice, not a change to FR-20-01):
  1st call  (no exit export yet)        -> creates it (status=pending), the caller schedules the
                                            background build, returns "offered", nothing deleted.
  later call, export not yet `ready`    -> 409 (`export_not_retrieved`) — the ordering guard: a
                                            delete attempted before the export is ready is refused.
  later call, export `ready`            -> runs the hard-delete cascade now, writes the immutable
                                            audit-trail entry, returns "completed", deleted=True.

`ready` IS "provided"/"retrieved" here: once the artifact is durably written and downloadable, the
system has provided it — the ticket names no separate "a human clicked download" signal, and a
backend cannot observe a browser download landing anyway (the existing `GET .../exports/{id}` poll,
reused unmodified, is how the FE checks/downloads it). `completed` is set on the in-memory row and
returned to the caller as a snapshot of what THIS transaction just did; the row itself is removed
moments later by the very cascade it triggers (a `DataExport.school_id` FK is NOT NULL — it cannot
outlive the school), so it is never observable via a later poll.
"""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.constants.enums import DataExportKind, DataExportStatus, StaffRole
from src.domain.compliance import services as compliance_db
from src.domain.compliance.models import DataExport
from src.domain.identity.models import StaffAccount

logger = logging.getLogger("youhue.compliance.deletion")

_EXPORT_NOT_RETRIEVED = HTTPException(
    status.HTTP_409_CONFLICT,
    detail={
        "code": "export_not_retrieved",
        "message": (
            "The full data export has not been provided yet — deletion is refused until it is "
            "ready."
        ),
    },
)


def request_export_and_delete(
    db: Session, actor: StaffAccount, school_id: uuid.UUID
) -> tuple[DataExport, bool, bool]:
    """Returns ``(row, deleted, needs_background_build)``. GATE-12-style authz: the role check runs
    BEFORE the school-scope check (mirrors FR-20-01's `request_export` exactly), so a non-leadership
    actor is denied the same way regardless of which school they target."""
    try:
        authz.require_roles(actor, StaffRole.leadership)
    except HTTPException:
        logger.warning(
            "fr_20_02_forbidden action=export_and_delete actor_id=%s reason=role", actor.id
        )
        raise
    if actor.school_id != school_id:
        logger.warning(
            "fr_20_02_forbidden action=export_and_delete actor_id=%s reason=cross_tenant "
            "school_id=%s",
            actor.id, school_id,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    existing = compliance_db.get_export_and_delete(db, school_id)

    if existing is None:
        try:
            export = compliance_db.create_export(
                db,
                school_id=school_id,
                requested_by=actor.id,
                kind=DataExportKind.export_and_delete,
            )
        except IntegrityError as exc:
            # A concurrent "start the exit" call for the SAME school won the race to
            # uq_data_exports_school_export_and_delete (mirrors submit_checkin's race guard,
            # src/application/checkin/services.py): converge on the SAME answer the sequential
            # "already started, not ready yet" path gives, rather than risk two callers each
            # believing THEY are the one driving a single-shot, irreversible delete.
            db.rollback()
            logger.info(
                "fr_20_02_rejected action=export_and_delete actor_id=%s school_id=%s "
                "reason=duplicate_exit event=race",
                actor.id, school_id,
            )
            raise _EXPORT_NOT_RETRIEVED from exc
        logger.info(
            "fr_20_02_success action=offer_export actor_id=%s school_id=%s export_id=%s",
            actor.id, school_id, export.id,
        )
        return export, False, True

    if existing.status != DataExportStatus.ready:
        logger.warning(
            "fr_20_02_rejected action=export_and_delete actor_id=%s school_id=%s export_id=%s "
            "status=%s",
            actor.id, school_id, existing.id, existing.status.value,
        )
        raise _EXPORT_NOT_RETRIEVED

    existing.status = DataExportStatus.completed
    db.flush()
    compliance_db.hard_delete_school_cascade(db, school_id)
    compliance_db.write_audit(
        db,
        actor_id=actor.id,
        action="fr_20_02.export_and_delete",
        target=f"school:{school_id}",
        # the school no longer exists after the cascade above — never re-point the FK at a gone
        # row; the UUID is preserved in `target` instead, so the immutable trail stays legible.
        school_id=None,
    )
    logger.info(
        "fr_20_02_success action=hard_delete actor_id=%s school_id=%s export_id=%s",
        actor.id, school_id, existing.id,
    )
    logger.info(
        "fr_20_02_audit action=hard_delete actor_id=%s school_id=%s", actor.id, school_id
    )
    return existing, True, False
