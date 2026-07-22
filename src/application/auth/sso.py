"""Staff SSO — real Google/Microsoft OAuth 2.0 / OIDC authorization-code flow.

A provider is *live* only when its client creds are present (``require_enabled`` -> 501 otherwise).
Two endpoints back the flow (see ``src.routers.staff_auth``):

  * **start**    -> ``build_authorization_redirect`` builds the provider authorize URL and the
                    browser is 302-redirected to the identity provider.
  * **callback** -> ``complete_sign_in`` verifies the signed ``state``, exchanges the ``code`` for
                    tokens, verifies the OIDC ``id_token`` and extracts the subject/email, then
                    ``resolve_or_link`` resolves it within the signing-in school. An SSO subject
                    ALREADY bound to an account signs in. A first-time subject whose email matches
                    an existing account is NOT auto-linked: the callback returns a ``link_required``
                    outcome carrying a short-lived single-use ``link_token`` and issues NO session.
  * **link**     -> ``complete_link`` verifies that token (signature, TTL, single-use) and ONLY
                    THEN binds the SSO subject to the account and issues the session. This explicit
                    confirm is the consent gate (Scenario 3); it stops the silent-link takeover.

Security: ``state`` is a short-lived signed JWT carrying {provider, school_id, nonce, jti} (CSRF +
tenant binding), and the ``jti`` is consumed once (``single_use``) so a captured state cannot be
replayed within its TTL. The OIDC ``id_token`` is signature-verified against the provider JWKS with
``iss``/``aud``/``exp``/``iat`` checked. Email is treated as verified ONLY when the provider asserts
``email_verified is True``; providers that omit the claim (Microsoft's ``id_token``) are NOT trusted
for LINKING an unverified email onto an existing password account. Email is unique *per school*, so
every resolve is school-scoped — an SSO identity can never resolve to (or link) an account in
another tenant.

The network calls to the live providers (token endpoint + JWKS) sit in ``_exchange_code`` /
``_verify_id_token`` and are exercised MANUALLY by the owner against real Google/Microsoft
(decision #1). Automated tests patch those two functions and drive the rest of the flow.
"""
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from config.env_config import settings
from src.application.auth import sessions, single_use
from src.constants.enums import SessionKind, StaffStatus
from src.domain.identity import services as identity_db
from src.schemas.auth import TokenResponse
from src.utils import security

PROVIDERS = ("google", "microsoft")
_STATE_TTL_MINUTES = 10
_LINK_TTL_MINUTES = 10
_GENERIC_401 = HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")


@dataclass(frozen=True)
class ProviderConfig:
    authorize_url: str
    token_url: str
    jwks_uri: str
    scope: str


PROVIDER_CONFIG: dict[str, ProviderConfig] = {
    "google": ProviderConfig(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",  # noqa: S106  (public endpoint URL)
        jwks_uri="https://www.googleapis.com/oauth2/v3/certs",
        scope="openid email profile",
    ),
    "microsoft": ProviderConfig(
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",  # noqa: S106
        jwks_uri="https://login.microsoftonline.com/common/discovery/v2.0/keys",
        scope="openid email profile",
    ),
}


# Expected id_token issuer per provider. Google pins an exact value; Microsoft ``/common/`` mints a
# per-tenant issuer, so it is validated against the tenant-issuer PATTERN instead of a fixed string.
_GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")
_MS_ISSUER_RE = re.compile(r"^https://login\.microsoftonline\.com/[0-9a-fA-F-]+/v2\.0$")


@dataclass(frozen=True)
class SsoClaims:
    subject: str
    email: str
    email_verified: bool


@dataclass(frozen=True)
class SessionIssued:
    """The SSO subject is (or has just been) bound to an account — a staff session was issued."""

    token: TokenResponse


@dataclass(frozen=True)
class LinkRequired:
    """The SSO email matches an existing account but the subject is not yet linked. No session is
    issued; the caller must confirm via ``POST /auth/staff/sso/link`` with this single-use token."""

    link_token: str
    email: str
    provider: str


SsoCallbackOutcome = SessionIssued | LinkRequired


# ---- provider gating ---------------------------------------------------------------------------
def is_enabled(provider: str) -> bool:
    if provider == "google":
        return bool(settings.google_client_id and settings.google_client_secret)
    if provider == "microsoft":
        return bool(settings.microsoft_client_id and settings.microsoft_client_secret)
    return False


def require_enabled(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown provider")
    if not is_enabled(provider):
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED, f"SSO provider '{provider}' is not configured"
        )


def _client_id(provider: str) -> str:
    return str(settings.google_client_id if provider == "google" else settings.microsoft_client_id)


def _client_secret(provider: str) -> str:
    return str(
        settings.google_client_secret if provider == "google" else settings.microsoft_client_secret
    )


def redirect_uri(provider: str) -> str:
    return f"{settings.oauth_redirect_base}{settings.api_prefix}/auth/staff/sso/{provider}/callback"


# ---- authorization redirect (step 1) ------------------------------------------------------------
def _sign_state(provider: str, school_id: uuid.UUID, nonce: str) -> str:
    """A signed, short-lived state token: CSRF protection + provider/tenant/nonce binding."""
    claims = {
        "purpose": "sso_state",
        "provider": provider,
        "school_id": str(school_id),
        "nonce": nonce,
        "jti": security.new_url_token(),  # single-use marker (consumed once on callback)
    }
    return security.encode_jwt(claims, datetime.now(UTC) + timedelta(minutes=_STATE_TTL_MINUTES))


def build_authorization_redirect(provider: str, school_id: uuid.UUID) -> str:
    """Build the provider authorize URL the browser is redirected to (302)."""
    cfg = PROVIDER_CONFIG[provider]
    nonce = security.new_url_token()
    state = _sign_state(provider, school_id, nonce)
    params = {
        "client_id": _client_id(provider),
        "response_type": "code",
        "redirect_uri": redirect_uri(provider),
        "scope": cfg.scope,
        "state": state,
        "nonce": nonce,
        "access_type": "offline",
        "prompt": "select_account",
    }
    return f"{cfg.authorize_url}?{urlencode(params)}"


def parse_state(provider: str, state: str) -> tuple[uuid.UUID, str]:
    """Verify the signed state belongs to THIS provider; return (school_id, nonce)."""
    try:
        payload = security.decode_jwt(state)
    except Exception as exc:  # noqa: BLE001  (any decode/signature/expiry failure -> generic 401)
        raise _GENERIC_401 from exc
    if payload.get("purpose") != "sso_state" or payload.get("provider") != provider:
        raise _GENERIC_401
    try:
        school_id = uuid.UUID(str(payload["school_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise _GENERIC_401 from exc
    jti = payload.get("jti")
    # single-use: a captured state cannot be replayed within its TTL (m3).
    if not jti or not single_use.consume(str(jti), _STATE_TTL_MINUTES * 60):
        raise _GENERIC_401
    return school_id, str(payload.get("nonce", ""))


# ---- token exchange + id_token verification (step 2; live-provider I/O) -------------------------
def _exchange_code(provider: str, code: str) -> dict[str, Any]:  # pragma: no cover - live provider
    """Exchange the authorization code for tokens at the provider token endpoint."""
    import httpx

    resp = httpx.post(
        PROVIDER_CONFIG[provider].token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(provider),
            "client_id": _client_id(provider),
            "client_secret": _client_secret(provider),
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return dict(resp.json())


def _iss_option(provider: str) -> dict[str, Any]:
    """Expected-issuer claim option per provider. Google pins exact values; Microsoft ``/common/``
    mints a per-tenant issuer so it is validated against the tenant-issuer PATTERN."""
    if provider == "google":
        return {"essential": True, "values": list(_GOOGLE_ISSUERS)}
    return {  # microsoft /common/
        "essential": True,
        "validate": lambda _claims, value: bool(value and _MS_ISSUER_RE.match(str(value))),
    }


def validate_id_token(provider: str, id_token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    """Verify the id_token signature against ``jwks`` and validate ``iss``/``aud``/``exp``/``iat``.

    Network-free (the JWKS is passed in) so the issuer/audience enforcement is unit-testable with a
    locally-signed token; ``_verify_id_token`` supplies the live provider JWKS."""
    from authlib.jose import JsonWebKey
    from authlib.jose import jwt as jose_jwt

    claims = jose_jwt.decode(
        id_token,
        JsonWebKey.import_key_set(jwks),
        claims_options={
            "iss": _iss_option(provider),
            "aud": {"essential": True, "value": _client_id(provider)},
        },
    )
    claims.validate()  # iss / exp / iat / aud
    return dict(claims)


def _verify_id_token(  # pragma: no cover - live provider
    provider: str, id_token: str
) -> dict[str, Any]:
    """Fetch the provider JWKS and verify the OIDC id_token (see ``validate_id_token``)."""
    import httpx

    jwks = httpx.get(PROVIDER_CONFIG[provider].jwks_uri, timeout=10.0).json()
    return validate_id_token(provider, id_token, dict(jwks))


def fetch_sso_identity(provider: str, code: str, nonce: str) -> SsoClaims:
    """Compose token-exchange + id_token verification into a verified (subject, email)."""
    tokens = _exchange_code(provider, code)
    id_token = tokens.get("id_token")
    if not id_token:
        raise _GENERIC_401
    claims = _verify_id_token(provider, str(id_token))
    if nonce and claims.get("nonce") not in (None, nonce):
        raise _GENERIC_401  # replayed / mismatched nonce
    subject = claims.get("sub")
    email = claims.get("email")
    if not subject or not email:
        raise _GENERIC_401  # provider must return a subject + email
    # Verified ONLY when the provider asserts it explicitly. An ABSENT claim (Microsoft omits it)
    # is NOT trusted as verified — never enough to LINK an email onto an existing account (M1).
    email_verified = claims.get("email_verified") is True
    return SsoClaims(subject=str(subject), email=str(email).lower(), email_verified=email_verified)


# ---- identity resolution (step 3) ---------------------------------------------------------------
def _issue_staff_session(db: Session, staff_id: uuid.UUID, school_id: uuid.UUID) -> TokenResponse:
    sess = sessions.create_session(
        db, staff_id, SessionKind.staff, settings.staff_session_ttl_minutes, school_id=school_id,
    )
    return TokenResponse(access_token=sessions.issue_token(sess))


def _sign_link_token(
    provider: str, sso_subject: str, account_id: uuid.UUID, school_id: uuid.UUID
) -> str:
    """A signed, short-lived, single-use token binding {provider, sso_subject, target account,
    nonce}. Presenting it back to ``complete_link`` is the explicit consent to bind the identity."""
    claims = {
        "purpose": "sso_link",
        "provider": provider,
        "sub": sso_subject,
        "account_id": str(account_id),
        "school_id": str(school_id),
        "nonce": security.new_url_token(),
        "jti": security.new_url_token(),  # single-use marker (consumed once on confirm)
    }
    return security.encode_jwt(claims, datetime.now(UTC) + timedelta(minutes=_LINK_TTL_MINUTES))


def resolve_or_link(
    db: Session,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
    school_id: uuid.UUID,
) -> SsoCallbackOutcome:
    """Resolve an SSO identity within ``school_id`` to a callback outcome.

    * subject ALREADY bound to an account -> sign in (``SessionIssued``); the binding is already
      trusted, so ``email_verified`` is not re-required here.
    * subject NOT bound, but ``email`` matches an existing active account -> do NOT auto-link and
      do NOT issue a session. Require a verified email (M1) and return ``LinkRequired`` with a
      single-use ``link_token`` for the explicit confirm step (M2). An unverified email is rejected
      generically so it can never initiate a link.
    * no matching account -> generic 401 (staff are provisioned elsewhere; no auto-provision)."""
    staff = identity_db.get_staff_by_sso_subject(db, subject, school_id)
    if staff is not None:
        return SessionIssued(_issue_staff_session(db, staff.id, staff.school_id))
    staff = identity_db.get_active_staff_by_email_in_school(db, email, school_id)
    if staff is None or not email_verified:
        raise _GENERIC_401
    return LinkRequired(
        link_token=_sign_link_token(provider, subject, staff.id, school_id),
        email=staff.email,
        provider=provider,
    )


def complete_sign_in(db: Session, provider: str, code: str, state: str) -> SsoCallbackOutcome:
    """Callback: verify state, exchange the code, resolve identity to a sign-in or link-required
    outcome. Never silently links — a first-time email match returns ``LinkRequired`` (M2)."""
    school_id, nonce = parse_state(provider, state)
    identity = fetch_sso_identity(provider, code, nonce)
    return resolve_or_link(
        db, provider, identity.subject, identity.email, identity.email_verified, school_id
    )


def complete_link(db: Session, link_token: str) -> TokenResponse:
    """Confirm step: verify the link token (signature, TTL, single-use), bind the SSO subject to the
    target account, and issue the staff session. This IS the consent gate for Scenario 3 — binding
    only happens here, never in the callback."""
    try:
        payload = security.decode_jwt(link_token)
    except Exception as exc:  # noqa: BLE001  (any decode/signature/expiry failure -> generic 401)
        raise _GENERIC_401 from exc
    if payload.get("purpose") != "sso_link":
        raise _GENERIC_401
    jti = payload.get("jti")
    # single-use: a captured/replayed confirm token is rejected (M2).
    if not jti or not single_use.consume(str(jti), _LINK_TTL_MINUTES * 60):
        raise _GENERIC_401
    provider = str(payload.get("provider", ""))
    subject = str(payload.get("sub", ""))
    if provider not in PROVIDERS or not subject:
        raise _GENERIC_401
    try:
        account_id = uuid.UUID(str(payload["account_id"]))
        school_id = uuid.UUID(str(payload["school_id"]))
    except (KeyError, ValueError, TypeError) as exc:
        raise _GENERIC_401 from exc
    staff = identity_db.get_staff(db, account_id)
    if staff is None or staff.school_id != school_id or staff.status != StaffStatus.active:
        raise _GENERIC_401
    # Defensive: never re-point an account already bound to a DIFFERENT subject, and never let a
    # subject that already belongs to another account in this school be re-bound here.
    if staff.sso_subject is not None and staff.sso_subject != subject:
        raise _GENERIC_401
    other = identity_db.get_staff_by_sso_subject(db, subject, school_id)
    if other is not None and other.id != staff.id:
        raise _GENERIC_401
    identity_db.link_sso_subject(db, staff, subject)
    return _issue_staff_session(db, staff.id, staff.school_id)


# ---- SSO callback delivery: HTML postMessage bridge (step 4) ------------------------------------
# The SPA runs the provider flow in a POPUP and holds tokens IN MEMORY ONLY (a top-level redirect
# would unload the SPA and wipe the token). So the callback does not return JSON -- it returns a
# tiny HTML page that hands the outcome to the opener via window.opener.postMessage(msg, ORIGIN)
# then window.close(). targetOrigin is the CONFIGURED frontend origin (never "*"): the session token
# must only ever be delivered to our own SPA, not to whatever page happens to own the popup.
_MESSAGE_TYPE = "youhue-sso"

_BRIDGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"><meta name="robots" content="noindex"><title>Signing you in</title>
</head>
<body>
<script>
(function () {
  var message = __MESSAGE__;
  var targetOrigin = __TARGET_ORIGIN__;
  try {
    if (window.opener) { window.opener.postMessage(message, targetOrigin); }
  } finally {
    window.close();
  }
})();
</script>
<p>You can close this window and return to Youhue.</p>
</body>
</html>
"""


def frontend_origin() -> str:
    """The scheme://host[:port] origin used as the postMessage targetOrigin.

    Derived from ``settings.frontend_base_url`` (path/query stripped to a bare origin). If it is
    unset or malformed we fail CLOSED with ``"null"`` (an opaque origin that matches nothing) rather
    than fall back to ``"*"`` -- a broadcast would leak the session token to any window."""
    parts = urlsplit((settings.frontend_base_url or "").strip())
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return "null"  # fail-closed: never broadcast the token with "*"


def _embed_json(obj: Any) -> str:
    """Serialize to JSON safe to inline inside a <script> block.

    ``json.dumps`` already escapes quotes/backslashes; on top of that we neutralise the sequences
    that could break OUT of the script element or be reinterpreted as HTML -- ``<`` / ``>`` (so a
    provider-derived value can never emit a closing script tag or an HTML comment), ``&``, and the
    JS line terminators U+2028 / U+2029. The embedded values are the server-issued token plus
    booleans and a fixed error string, but we escape unconditionally so the bridge is
    injection-proof by construction."""
    return (
        json.dumps(obj, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


def callback_bridge_response(
    *,
    ok: bool,
    access_token: str | None = None,
    token_type: str = "bearer",  # noqa: S107  (OAuth token-type label, not a secret)
    mfa_required: bool = False,
    error: str | None = None,
    link_required: bool = False,
    link_token: str | None = None,
    provider: str | None = None,
    email: str | None = None,
) -> HTMLResponse:
    """Build the ``text/html`` postMessage bridge (HTTP 200) for a callback outcome."""
    if ok:
        message: dict[str, Any] = {
            "type": _MESSAGE_TYPE,
            "ok": True,
            "access_token": access_token,
            "token_type": token_type,
            "mfa_required": mfa_required,
        }
    else:
        message = {
            "type": _MESSAGE_TYPE,
            "ok": False,
            "error": error or "Sign-in failed",
            "link_required": link_required,
            "link_token": link_token,
            "provider": provider,
            "email": email,
        }
    html = _BRIDGE_HTML.replace("__MESSAGE__", _embed_json(message)).replace(
        "__TARGET_ORIGIN__", _embed_json(frontend_origin())
    )
    return HTMLResponse(content=html, status_code=status.HTTP_200_OK)
