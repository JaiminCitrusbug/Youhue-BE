"""Admin-console RBAC policy (FR-19-01, decision #3) — SRS §8 for the internal team.

The single policy layer every admin-console feature (FR-19-02/04/05/07) attaches to later: a
permission -> allowed-roles matrix, decided server-side, tested as the disallowed actor (403).
A denial is written to the immutable audit log before the 403 is raised — never an open backdoor.
Reuses the INFRA-03 authz pattern (`require_*` raises 403) and the compliance audit sink; it does
NOT invent a parallel auth system.
"""
import enum

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.constants.enums import AdminRole
from src.domain.compliance import services as audit_db
from src.domain.identity.models import InternalAdmin


class AdminPermission(str, enum.Enum):
    """Representative internal-team capabilities. Real admin features bind to these later:
    FR-19-02 -> manage_accounts, FR-19-04 -> manage_seed, FR-19-05 -> manage_word_lists,
    FR-19-07 -> view_statistics; platform_config is superadmin-only platform maintenance."""

    manage_accounts = "manage_accounts"
    manage_seed = "manage_seed"
    manage_word_lists = "manage_word_lists"
    view_statistics = "view_statistics"
    platform_config = "platform_config"


# The role -> permission matrix. superadmin has every permission; support is a strict subset
# (school-support only) — it CANNOT do seed / word-list / platform maintenance. That gap is the
# deny-cell the RBAC probe route exercises now (FR-19-04 seed maintenance is gated the same way).
_ROLE_PERMISSIONS: dict[AdminRole, frozenset[AdminPermission]] = {
    AdminRole.superadmin: frozenset(AdminPermission),
    AdminRole.support: frozenset(
        {AdminPermission.manage_accounts, AdminPermission.view_statistics}
    ),
}


def has_permission(role: AdminRole, permission: AdminPermission) -> bool:
    return permission in _ROLE_PERMISSIONS.get(role, frozenset())


def require_permission(
    db: Session, admin: InternalAdmin, permission: AdminPermission
) -> None:
    """Admin RBAC gate. Raises 403 for the disallowed actor and audit-logs the denial first
    (immutable AuditLog, platform-level). The caller commits so the record survives the 403."""
    if not has_permission(admin.role, permission):
        audit_db.write_audit(
            db,
            actor_id=admin.id,
            action=f"admin.rbac.denied:{permission.value}",
            target="admin_console",
            school_id=None,
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
