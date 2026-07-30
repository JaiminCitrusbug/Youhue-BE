"""Admin console router (FR-19-01) — its own surface (decision #4), separate from staff/student.

Endpoints:
  POST  /api/v1/admin/sign-in            email + password + (email-OTP) MFA -> admin session + role
  GET   /api/v1/admin/concern-words/default   read the platform default concern-word list (FR-19-05)
  PUT   /api/v1/admin/concern-words/default   replace it (both `manage_word_lists`-gated)
  GET   /api/v1/admin/schools                every school (SC-075, `manage_accounts`-gated)
  GET   /api/v1/admin/schools/{id}           one school + its trial state (SC-076/077)
  PATCH /api/v1/admin/schools/{id}           extend_trial | update_account | support_access
        (FR-19-02) — 200 on success; 409 the school's one trial extension is already used; 403 the
        actor's role lacks the action's permission (audit-logged); 500 on a persistence failure.
  GET   /api/v1/admin/audit-log              filter (actor/action/school/date) + paginate the
        immutable audit trail (FR-20-05, SC-080) — read-only, `view_audit_log`-gated.
  GET   /api/v1/admin/audit-log/export       the same filtered set as a CSV extract — no
        write/patch/delete endpoint exists on this table at any layer.
  GET   /api/v1/admin/_probe/seed-maintenance
        a representative role-gated probe (decision #3) so the NEGATIVE 403 scenario is genuinely
        exercised now: a permitted role -> 200, a limited role -> 403 (audit-logged). Real admin
        features (FR-19-04/07) attach to this same RBAC policy layer later.

Thin router — business logic lives in src.application.*. Sign-in is generic-error, rate-limited,
and lockout-protected.
"""
import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from src.application.audit_log import services as audit_log_svc
from src.application.audit_log.services import AuditLogQuery
from src.application.auth import admin as admin_svc
from src.application.authz import admin as admin_authz
from src.application.concern_words import services as concern_words_svc
from src.application.platform_stats import services as platform_stats_svc
from src.application.school_admin import services as school_admin_svc
from src.application.seed import services as seed_svc
from src.constants.enums import SessionKind
from src.domain.billing.models import Subscription
from src.domain.identity.models import InternalAdmin, School
from src.infrastructure.middlewares.auth_middleware import DbDep, SessionDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.admin import (
    AdminSchoolAction,
    AdminSignIn,
    AdminSignInResponse,
    AuditLogEntry,
    AuditLogListResponse,
    DefaultWordListResponse,
    DefaultWordListUpdate,
    PlatformStatsResponse,
    SchoolActionRequest,
    SchoolActionResponse,
    SchoolDetail,
    SchoolListItem,
    SchoolsListResponse,
    SeedActivityCreate,
    SeedActivityListResponse,
    SeedActivityResponse,
    SeedActivityUpdate,
)

router = APIRouter(prefix="/admin", tags=["admin"])
_throttle = [Depends(rate_limit)]
logger = logging.getLogger("youhue.admin.schools")


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


def _school_detail(school: School, sub: Subscription | None) -> SchoolDetail:
    return SchoolDetail(
        id=school.id,
        name=school.name,
        tier=school.tier,
        status=school.status,
        timezone=school.timezone,
        subscription_state=sub.state if sub else None,
        trial_end_at=sub.trial_end_at if sub else None,
        trial_extension_count=sub.trial_extension_count if sub else 0,
    )


@router.get("/stats", response_model=PlatformStatsResponse)
def get_platform_stats(admin: AdminDep, db: DbDep) -> PlatformStatsResponse:
    """SC-074 — platform-wide statistics: schools, active trials, check-in volume, alert volume.
    `view_statistics`-gated (403 audit-logged otherwise); read-only aggregates over base tables,
    rendered not recomputed. An empty platform returns all-zero counts (the empty state), never an
    error."""
    try:
        stats = platform_stats_svc.get_platform_stats(db, admin)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the error
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort guard, never leak an unhandled 500
        db.rollback()
        logger.exception("fr_19_07_error admin=%s action=get_platform_stats", admin.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not load platform statistics"
        ) from exc
    return PlatformStatsResponse(
        schools=stats.schools,
        active_trials=stats.active_trials,
        checkin_volume=stats.checkin_volume,
        alert_volume=stats.alert_volume,
    )


# ---- FR-20-05: audit-log viewer (SC-080) — read-only, `view_audit_log`-gated -----------------
# No PATCH/PUT/DELETE route exists on this table at any layer; the DB-level append-only triggers
# (migration c0ffee000001) additionally block UPDATE/DELETE server-side regardless.

def _audit_query(
    actor_id: uuid.UUID | None,
    action: str | None,
    school_id: uuid.UUID | None,
    date_from: datetime | None,
    date_to: datetime | None,
    page: int,
    page_size: int,
) -> AuditLogQuery:
    return AuditLogQuery(
        actor_id=actor_id,
        action=action,
        school_id=school_id,
        date_from=date_from,
        date_to=date_to,
        page=max(page, 1),
        page_size=max(1, min(page_size, audit_log_svc.MAX_PAGE_SIZE)),
    )


@router.get("/audit-log", response_model=AuditLogListResponse)
def list_audit_log(
    admin: AdminDep,
    db: DbDep,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    school_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: int = 1,
    page_size: int = 50,
) -> AuditLogListResponse:
    """SC-080 — filter + paginate the immutable audit trail, newest first. `view_audit_log`-gated
    (403, audit-logged, otherwise)."""
    query = _audit_query(actor_id, action, school_id, date_from, date_to, page, page_size)
    try:
        rows, total = audit_log_svc.list_audit_log(db, admin, query)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the error
        raise
    return AuditLogListResponse(
        entries=[AuditLogEntry.model_validate(r) for r in rows],
        total=total,
        page=query.page,
        page_size=query.page_size,
    )


@router.get("/audit-log/export")
def export_audit_log(
    admin: AdminDep,
    db: DbDep,
    actor_id: uuid.UUID | None = None,
    action: str | None = None,
    school_id: uuid.UUID | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
) -> Response:
    """SC-080's "Export" control — the SAME filter as the list, as a CSV extract. Same
    `view_audit_log` gate; capped at `MAX_EXPORT_ROWS` rows."""
    query = _audit_query(
        actor_id, action, school_id, date_from, date_to, 1, audit_log_svc.MAX_EXPORT_ROWS
    )
    try:
        csv_body = audit_log_svc.export_audit_log(db, admin, query)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the error
        raise
    return Response(
        content=csv_body,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=audit-log.csv"},
    )


@router.get("/schools", response_model=SchoolsListResponse)
def list_schools(admin: AdminDep, db: DbDep, q: str | None = None) -> SchoolsListResponse:
    """SC-075: every school on the platform, optionally name-filtered. `manage_accounts`-gated."""
    try:
        schools = school_admin_svc.list_schools(db, admin, q)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial before surfacing the error
        raise
    items = [SchoolListItem(id=s.id, name=s.name, tier=s.tier, status=s.status) for s in schools]
    return SchoolsListResponse(schools=items, count=len(schools))


@router.get("/schools/{school_id}", response_model=SchoolDetail)
def get_school(school_id: uuid.UUID, admin: AdminDep, db: DbDep) -> SchoolDetail:
    """SC-076/077: one school's account + trial state. `manage_accounts`-gated."""
    try:
        school, sub = school_admin_svc.get_school(db, admin, school_id)
    except HTTPException:
        db.commit()  # persist the audit-logged RBAC denial (or surface the 404) before raising
        raise
    return _school_detail(school, sub)


@router.patch("/schools/{school_id}", response_model=SchoolActionResponse)
def patch_school(
    school_id: uuid.UUID, body: SchoolActionRequest, admin: AdminDep, db: DbDep
) -> SchoolActionResponse:
    """FR-19-02: `extend_trial` (409 once already used) | `update_account` | `support_access`
    (permission-bound + audit-logged). Each action's own permission gate raises 403 (audit-logged)
    for a role that lacks it; the router commits before every raise so denial/409/404 rows survive.
    An unexpected persistence failure is caught and surfaced as 500 (never a raw 500 with no body,
    never silently swallowed) — the ticket's `500 { error }` case."""
    sub: Subscription | None
    try:
        if body.action == AdminSchoolAction.extend_trial:
            school, sub = school_admin_svc.extend_trial(db, admin, school_id)
        elif body.action == AdminSchoolAction.update_account:
            school, sub = school_admin_svc.update_account(
                db, admin, school_id,
                tier=body.tier, account_status=body.status, timezone=body.timezone,
            )
        else:
            school, sub = school_admin_svc.support_access(
                db, admin, school_id, reason=body.reason
            )
    except HTTPException:
        db.commit()
        raise
    except Exception:
        db.rollback()
        logger.exception(
            "fr_19_02_error admin=%s action=%s school=%s", admin.id, body.action, school_id
        )
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not process the request"
        ) from None
    db.commit()
    return SchoolActionResponse(action=body.action, school=_school_detail(school, sub))


# ---- FR-19-04: maintain the platform seed activity set (gated by `manage_seed`) ----
# Every write commits on success (persist the activity + its audit record) and on the 403 path
# (persist the audit-logged RBAC denial before surfacing the error) — errors are never swallowed.

@router.post("/seed-activities", response_model=SeedActivityResponse)
def create_seed_activity(
    body: SeedActivityCreate, admin: AdminDep, db: DbDep
) -> SeedActivityResponse:
    try:
        activity = seed_svc.create_seed_activity(db, admin, body)
    except HTTPException:
        db.commit()
        raise
    db.commit()
    return SeedActivityResponse(activity=activity)  # type: ignore[arg-type]


@router.get("/seed-activities", response_model=SeedActivityListResponse)
def list_seed_activities(
    admin: AdminDep, db: DbDep, include_retired: bool = False
) -> SeedActivityListResponse:
    try:
        activities = seed_svc.list_seed_activities(db, admin, include_retired=include_retired)
    except HTTPException:
        db.commit()
        raise
    return SeedActivityListResponse.of(activities)


@router.patch("/seed-activities/{activity_id}", response_model=SeedActivityResponse)
def edit_seed_activity(
    activity_id: uuid.UUID, body: SeedActivityUpdate, admin: AdminDep, db: DbDep
) -> SeedActivityResponse:
    try:
        activity = seed_svc.edit_seed_activity(db, admin, activity_id, body)
    except HTTPException:
        db.commit()
        raise
    db.commit()
    return SeedActivityResponse(activity=activity)  # type: ignore[arg-type]


@router.delete("/seed-activities/{activity_id}", response_model=SeedActivityResponse)
def retire_seed_activity(
    activity_id: uuid.UUID, admin: AdminDep, db: DbDep
) -> SeedActivityResponse:
    try:
        activity = seed_svc.retire_seed_activity(db, admin, activity_id)
    except HTTPException:
        db.commit()
        raise
    db.commit()
    return SeedActivityResponse(activity=activity)  # type: ignore[arg-type]
