"""Admin console router (FR-19-01) — its own surface (decision #4), separate from staff/student.

Endpoints:
  POST /api/v1/admin/sign-in            email + password + (email-OTP) MFA -> admin session + role
  GET  /api/v1/admin/concern-words/default   read the platform default concern-word list (FR-19-05)
  PUT  /api/v1/admin/concern-words/default   replace it (both `manage_word_lists`-gated)
  GET  /api/v1/admin/_probe/seed-maintenance
        a representative role-gated probe (decision #3) so the NEGATIVE 403 scenario is genuinely
        exercised now: a permitted role -> 200, a limited role -> 403 (audit-logged). Real admin
        features (FR-19-02/04/05/07) attach to this same RBAC policy layer later.

Thin router — business logic lives in src.application.*. Sign-in is generic-error, rate-limited,
and lockout-protected.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from src.application.auth import admin as admin_svc
from src.application.authz import admin as admin_authz
from src.application.concern_words import services as concern_words_svc
from src.constants.enums import SessionKind
from src.domain.identity.models import InternalAdmin
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.admin import (
    AdminSignIn,
    AdminSignInResponse,
    DefaultWordListResponse,
    DefaultWordListUpdate,
)

router = APIRouter(prefix="/admin", tags=["admin"])
_throttle = [Depends(rate_limit)]


def get_current_admin(sess: SessionDep, db: DbDep) -> InternalAdmin:
    """Resolve the internal admin behind an admin-kind session. A staff/student session can never
    reach an admin route (403), mirroring the student->staff surface separation."""
    if sess.kind != SessionKind.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")
    admin = db.get(InternalAdmin, sess.subject_id)
    if admin is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No account")
    return admin


AdminDep = Annotated[InternalAdmin, Depends(get_current_admin)]


@router.post("/sign-in", response_model=AdminSignInResponse, dependencies=_throttle)
def admin_sign_in(body: AdminSignIn, db: DbDep) -> AdminSignInResponse:
    try:
        result = admin_svc.sign_in(db, body.email, body.password, body.mfa_code, body.device_id)
    except HTTPException:
        db.commit()  # persist failed-attempt / lockout records before surfacing the error
        raise
    db.commit()
    return result


@router.get("/_probe/seed-maintenance")
def probe_seed_maintenance(admin: AdminDep, db: DbDep) -> dict[str, str]:
    """Role-gated probe: requires the `manage_seed` permission (a limited support role lacks it)."""
    try:
        admin_authz.require_permission(db, admin, admin_authz.AdminPermission.manage_seed)
    except HTTPException:
        db.commit()  # persist the audit-logged denial before surfacing the 403
        raise
    return {"status": "ok", "action": "seed-maintenance", "role": admin.role.value}


@router.get("/concern-words/default", response_model=DefaultWordListResponse)
def read_default_concern_words(admin: AdminDep, db: DbDep) -> DefaultWordListResponse:
    """FR-19-05: read the platform DEFAULT concern-word list. Same `manage_word_lists` gate as the
    PUT (a role without it -> 403, audit-logged; no/non-admin session -> 403). The editor loads
    this on mount so entries can be EDITED and REMOVED rather than replaced blind — without it a
    full-replacement PUT would silently drop every word the admin never saw. `words` is empty only
    when no default has been seeded yet."""
    try:
        words = concern_words_svc.get_default(db, admin)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the error
        raise
    return DefaultWordListResponse(words=words, count=len(words))


@router.put("/concern-words/default", response_model=DefaultWordListResponse)
def update_default_concern_words(
    body: DefaultWordListUpdate, admin: AdminDep, db: DbDep
) -> DefaultWordListResponse:
    """FR-19-05: maintain the platform DEFAULT concern-word list. `manage_word_lists`-gated
    (a role without it -> 403, audit-logged). GATE G-6: schools that have overridden the default
    are unaffected — only the is_default row is written. Applies to schools with no override."""
    try:
        words = concern_words_svc.update_default(db, admin, body.words)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the error
        raise
    db.commit()
    return DefaultWordListResponse(words=words, count=len(words))
