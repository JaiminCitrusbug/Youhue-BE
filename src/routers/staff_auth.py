"""Staff auth surface (FR-01-03): email/password sign-in, email-OTP MFA, single-use expiring
password reset, and real Google/Microsoft SSO (OAuth2/OIDC).

Split out of the shared ``src.routers.auth`` router (decision #4) so the staff module is
independently ownable; admin (Lane C) gets its own module the same way. Behaviour is identical to
the INFRA-01 endpoints it decomposes — only the module boundary moved. Thin router: all business
logic lives in ``src.application.auth.*``. Sign-in / OTP / forgot / SSO-start are throttled; sign-in
errors are generic (401); forgot-password is 202 with no account-existence disclosure.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from src.application.auth import sso as sso_svc
from src.application.auth import staff as staff_svc
from src.domain.identity import services as identity_db
from src.infrastructure.middlewares.auth_middleware import DbDep
from src.infrastructure.middlewares.ratelimit import rate_limit
from src.schemas.auth import TokenResponse
from src.schemas.staff_auth import ForgotPassword, MfaVerify, ResetPassword, StaffSignIn

router = APIRouter(prefix="/auth/staff", tags=["staff-auth"])
_throttle = [Depends(rate_limit)]


@router.post("/sign-in", response_model=TokenResponse, dependencies=_throttle)
def staff_sign_in(body: StaffSignIn, db: DbDep) -> TokenResponse:
    result = staff_svc.sign_in(db, body.email, body.password, body.device_id)
    db.commit()
    return result


@router.post("/mfa/verify", response_model=TokenResponse, dependencies=_throttle)
def staff_mfa_verify(body: MfaVerify, db: DbDep) -> TokenResponse:
    try:
        result = staff_svc.verify_mfa_and_promote(db, body.session_token, body.code)
    except HTTPException:
        db.commit()  # persist any failed-attempt record (brute-force cap)
        raise
    db.commit()
    return result


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED, dependencies=_throttle)
def forgot_password(body: ForgotPassword, db: DbDep) -> dict[str, str]:
    staff_svc.forgot_password(db, body.email)
    db.commit()
    return {"status": "accepted"}


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(body: ResetPassword, db: DbDep) -> Response:
    staff_svc.reset_password(db, body.token, body.new_password)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/sso/{provider}", dependencies=_throttle)
def sso_start(
    provider: str, db: DbDep, school_code: str | None = Query(default=None)
) -> RedirectResponse:
    """Step 1: 302 the browser to the identity provider. The school context (needed for the
    tenant-scoped resolve on callback) is carried in the signed ``state``. Provider gating
    (404 unknown / 501 unconfigured) runs BEFORE any tenant lookup."""
    sso_svc.require_enabled(provider)
    if not school_code:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "school_code is required")
    school = identity_db.get_active_school_by_code(db, school_code)
    if school is None:
        # generic 401 — no account-existence / tenant disclosure
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")
    url = sso_svc.build_authorization_redirect(provider, school.id)
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/sso/{provider}/callback")
def sso_callback(
    provider: str, db: DbDep, code: str = Query(...), state: str = Query(...)
) -> HTMLResponse:
    """Step 2: exchange code, verify id_token, resolve/link to one staff identity, sign in.

    Delivery is an HTML **postMessage bridge**, not JSON: the SPA runs SSO in a popup and holds the
    token in memory only, so the callback hands the outcome to ``window.opener`` (targeting the
    configured frontend origin) then closes the popup. Provider gating (404/501) still raises before
    the bridge; the auth outcome (success or a generic sign-in failure) is delivered via the bridge
    so the popup can always postMessage back (a JSON error body could not run the bridge script)."""
    sso_svc.require_enabled(provider)
    try:
        result = sso_svc.complete_sign_in(db, provider, code, state)
        db.commit()
    except HTTPException as exc:
        db.rollback()
        return sso_svc.callback_bridge_response(ok=False, error=str(exc.detail))
    return sso_svc.callback_bridge_response(
        ok=True,
        access_token=result.access_token,
        token_type=result.token_type,
        mfa_required=result.mfa_required,
    )
