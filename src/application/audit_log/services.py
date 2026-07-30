"""Admin-console audit-log viewer (FR-20-05, SC-080) — read-only over the append-only `audit_logs`
store. INFRA-02's DB-level triggers (migration c0ffee000001) block UPDATE/DELETE on `audit_logs`
at the database itself; this module adds NO write/patch/delete path on top of that — filtered
reads + a CSV extract only, `view_audit_log`-gated (a role without it -> 403, audit-logged).
"""
import csv
import io
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.application.authz import admin as admin_authz
from src.domain.compliance import services as compliance_db
from src.domain.compliance.models import AuditLog
from src.domain.identity.models import InternalAdmin

logger = logging.getLogger("youhue.admin.audit_log")

MAX_PAGE_SIZE = 200
# The export is a synchronous, in-memory CSV response (no async job/object-storage machinery —
# FR-20-01's DataExport model is school-scoped self-service data and does not fit this platform-
# wide, admin-only extract). Capped so a filter-less export can't be used to dump the whole table
# in one request.
MAX_EXPORT_ROWS = 5000


@dataclass
class AuditLogQuery:
    actor_id: uuid.UUID | None = None
    action: str | None = None
    school_id: uuid.UUID | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None
    page: int = 1
    page_size: int = 50


def _require(db: Session, admin: InternalAdmin, action: str) -> None:
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.view_audit_log)
    except HTTPException:
        logger.warning("fr_20_05_forbidden admin=%s action=%s", admin.id, action)
        raise


def list_audit_log(
    db: Session, admin: InternalAdmin, q: AuditLogQuery
) -> tuple[list[AuditLog], int]:
    """SC-080 — filter (actor/action/school/date) + paginate the trail, newest first."""
    _require(db, admin, "list_audit_log")
    rows, total = compliance_db.list_audit_logs(
        db,
        actor_id=q.actor_id,
        action=q.action,
        school_id=q.school_id,
        date_from=q.date_from,
        date_to=q.date_to,
        page=q.page,
        page_size=q.page_size,
    )
    logger.info(
        "fr_20_05_success admin=%s action=list_audit_log count=%s total=%s",
        admin.id, len(rows), total,
    )
    return rows, total


def export_audit_log(db: Session, admin: InternalAdmin, q: AuditLogQuery) -> str:
    """The SAME filtered query as `list_audit_log`, rendered as CSV (the SC-080 "Export" control).
    Read-only — building a text extract from already-read rows writes nothing new."""
    _require(db, admin, "export_audit_log")
    rows, _total = compliance_db.list_audit_logs(
        db,
        actor_id=q.actor_id,
        action=q.action,
        school_id=q.school_id,
        date_from=q.date_from,
        date_to=q.date_to,
        page=1,
        page_size=MAX_EXPORT_ROWS,
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["at", "actor_id", "action", "target", "school_id"])
    for row in rows:
        writer.writerow(
            [row.at.isoformat(), row.actor_id, row.action, row.target, row.school_id or ""]
        )
    logger.info(
        "fr_20_05_success admin=%s action=export_audit_log count=%s", admin.id, len(rows),
    )
    return buf.getvalue()
