"""Default concern-word list maintenance (FR-19-05) — the internal team's platform DEFAULT.

The admin console maintains the ONE platform default list that INFRA-06 concern-word scoring
(FR-12-01) falls back to when a school has no override. GATE G-6: a school's OWN override always
takes precedence — this service only ever reads/writes the is_default row, so a default change can
never reach into or overwrite a school override.

RBAC-gated by the FR-19-01 admin policy (`manage_word_lists`); a role without it is denied 403
(audit-logged). Validation is server-side authoritative; the write is ACID + idempotent on retry.
No word content is logged (no PII/PHI) — only actor id + counts.
"""
import logging

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import admin as admin_authz
from src.domain.identity.models import InternalAdmin
from src.domain.risk import services as risk_db

logger = logging.getLogger("youhue.admin.wordlists")

MAX_WORDS = 1000
MAX_WORD_LEN = 100


def _normalize(words: list[str]) -> list[str]:
    """Trim + lowercase, drop blanks, de-dupe (order-preserving). Matching is case-insensitive and
    whole-word (INFRA-06), so a normalized canonical form keeps the stored default clean."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in words:
        w = raw.strip().lower()
        if w and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _validate(words: list[str]) -> list[str]:
    """Authoritative server-side validation. Raises 422 (surfaced, never silently dropped)."""
    if len(words) > MAX_WORDS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Too many entries")
    if any(len(raw) > MAX_WORD_LEN for raw in words):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Entry too long")
    cleaned = _normalize(words)
    if not cleaned:
        # An empty platform default would disable concern-word detection for EVERY school without an
        # override — refuse it (a school opts out via its own empty override, never the platform).
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "The default list must have at least one entry"
        )
    return cleaned


def get_default(db: Session, admin: InternalAdmin) -> list[str]:
    """Read the platform default concern-word list so the admin console can EDIT and REMOVE
    entries, not only add to a blank set (FR-19-05 Scenario 1). Same `manage_word_lists` gate as
    the write — the maintenance surface is one capability, so read and write share one permission
    and a role without it is denied 403 (audit-logged) on both.

    Returns [] when the internal team has not seeded a default yet; that is a real, distinguishable
    state (nothing to destroy), NOT a load failure — a failure raises instead of returning [].
    Caller commits so the RBAC-denial audit row survives. No word content is logged.
    """
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_word_lists)
    except HTTPException:
        logger.warning("fr_19_05_forbidden admin=%s action=read", admin.id)
        raise

    try:
        row = risk_db.get_default_concern_word_list(db)
    except Exception:
        logger.exception("fr_19_05_error admin=%s action=read", admin.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not read the default list"
        ) from None

    words = list(row.words) if row is not None else []
    logger.info("fr_19_05_success admin=%s action=read count=%s", admin.id, len(words))
    return words


def update_default(db: Session, admin: InternalAdmin, words: list[str]) -> list[str]:
    """Maintain the platform default concern-word list (add/edit/remove = the full replacement set).
    Returns the persisted, normalized words. Caller commits so the RBAC-denial audit row survives.
    """
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_word_lists)
    except HTTPException:
        logger.warning("fr_19_05_forbidden admin=%s", admin.id)
        raise

    try:
        cleaned = _validate(words)
    except HTTPException as exc:
        logger.warning("fr_19_05_rejected admin=%s status=%s", admin.id, exc.status_code)
        raise

    try:
        row = risk_db.set_default_concern_word_list(db, cleaned)
    except Exception:
        logger.exception("fr_19_05_error admin=%s", admin.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not update the default list"
        ) from None

    logger.info("fr_19_05_success admin=%s count=%s", admin.id, len(row.words))
    return list(row.words)
