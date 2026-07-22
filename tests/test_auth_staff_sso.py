"""Staff SSO (Google/Microsoft OAuth2/OIDC) — FR-01-03 Scenarios 2, 3 and 5.

The flow is exercised against a MOCKED provider (decision #1): the live token-exchange +
id_token-verification (`sso._exchange_code` / `sso._verify_id_token`) are patched, so no test ever
hits Google/Microsoft. The owner tests the live path manually. Everything else — state signing/CSRF,
resolve-or-link, session issuance, provider gating — runs for real.
"""
import json
import re

import src.application.auth.sso as sso
import src.application.auth.staff as staff_mod
from src.domain.auth.models import PasswordResetToken
from src.domain.identity.services import get_staff_by_sso_subject
from src.utils.security import decode_jwt

SSO = "/api/v1/auth/staff/sso"
SIGNIN = "/api/v1/auth/staff/sign-in"


def _bridge_message(resp) -> dict:
    """Parse the postMessage envelope embedded in the HTML callback bridge."""
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    m = re.search(r"var message = (\{.*?\});", resp.text)
    assert m, f"no postMessage envelope in bridge HTML:\n{resp.text}"
    return json.loads(m.group(1))  # json.loads decodes the \\u003c-style safe escapes


def _bridge_target_origin(resp) -> str:
    m = re.search(r"var targetOrigin = (\".*?\");", resp.text)
    assert m, "no targetOrigin in bridge HTML"
    return json.loads(m.group(1))


def _enable(monkeypatch, provider="google"):
    monkeypatch.setattr(sso.settings, f"{provider}_client_id", f"{provider}-cid")
    monkeypatch.setattr(sso.settings, f"{provider}_client_secret", f"{provider}-secret")


def _mock_provider(monkeypatch, *, sub, email, email_verified=True):
    monkeypatch.setattr(sso, "_exchange_code", lambda provider, code: {"id_token": "fake.jwt"})
    monkeypatch.setattr(
        sso, "_verify_id_token",
        lambda provider, id_token: {"sub": sub, "email": email, "email_verified": email_verified},
    )


def _callback(client, provider, state, code="auth-code"):
    return client.get(f"{SSO}/{provider}/callback", params={"code": code, "state": state})


# ---- provider gating (501 / 404) ----------------------------------------------------------------
def test_sso_start_unconfigured_returns_501(client):
    r = client.get(f"{SSO}/google", params={"school_code": "SCH-001"}, follow_redirects=False)
    assert r.status_code == 501  # no creds present -> provider not live


def test_sso_start_unknown_provider_404(client):
    r = client.get(f"{SSO}/github", params={"school_code": "SCH-001"}, follow_redirects=False)
    assert r.status_code == 404


# ---- authorization redirect (302) ---------------------------------------------------------------
def test_sso_start_redirects_to_provider(client, make_school, monkeypatch):
    _enable(monkeypatch, "google")
    make_school(code="SCH-001")
    r = client.get(f"{SSO}/google", params={"school_code": "SCH-001"}, follow_redirects=False)
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "client_id=google-cid" in loc and "state=" in loc and "response_type=code" in loc


def test_sso_start_unknown_school_is_generic_401(client, monkeypatch):
    _enable(monkeypatch, "microsoft")
    r = client.get(f"{SSO}/microsoft", params={"school_code": "NOPE"}, follow_redirects=False)
    assert r.status_code == 401  # no account-existence / tenant disclosure


def test_sso_start_requires_school_code(client, monkeypatch):
    _enable(monkeypatch, "google")  # provider is live, but no school context supplied
    r = client.get(f"{SSO}/google", follow_redirects=False)
    assert r.status_code == 400


# ---- callback: sign-in success (Scenario 2) -----------------------------------------------------
def test_sso_sign_in_success_when_already_linked(client, db, make_school, make_staff, monkeypatch):
    _enable(monkeypatch, "google")
    school = make_school(code="SCH-001")
    staff = make_staff(school, email="head@oakwood.edu", password="Password123")
    staff.sso_subject = "google-sub-1"  # already linked
    db.commit()
    _mock_provider(monkeypatch, sub="google-sub-1", email="head@oakwood.edu")

    state = sso._sign_state("google", school.id, "n1")
    r = _callback(client, "google", state)
    msg = _bridge_message(r)  # 200 text/html postMessage bridge (not JSON)
    assert msg["ok"] is True
    assert msg["type"] == "youhue-sso"
    assert msg["token_type"] == "bearer"
    assert msg["mfa_required"] is False
    assert decode_jwt(msg["access_token"])["sub"] == str(staff.id)


def test_sso_callback_delivers_html_postmessage_bridge_to_configured_origin(
    client, db, make_school, make_staff, monkeypatch
):
    """The callback returns an HTML postMessage bridge targeting the CONFIGURED frontend origin
    (never "*"), embedding the issued session token — the popup delivery contract (FR-01-03)."""
    _enable(monkeypatch, "google")
    school = make_school(code="SCH-001")
    staff = make_staff(school, email="head@oakwood.edu", password="Password123")
    staff.sso_subject = "google-sub-1"
    db.commit()
    _mock_provider(monkeypatch, sub="google-sub-1", email="head@oakwood.edu")

    r = _callback(client, "google", sso._sign_state("google", school.id, "n1"))
    assert r.headers["content-type"].startswith("text/html")
    assert "postMessage" in r.text and "window.close()" in r.text
    # targetOrigin is the configured frontend origin, never a wildcard broadcast.
    origin = _bridge_target_origin(r)
    assert origin == sso.frontend_origin()
    assert origin.startswith("http://localhost:5173")
    assert '"*"' not in r.text and "postMessage(message, \"*\")" not in r.text
    # success embeds the server-issued token so the opener can store it in memory.
    msg = _bridge_message(r)
    assert decode_jwt(msg["access_token"])["sub"] == str(staff.id)


# ---- callback: first-time link resolves to ONE identity (Scenario 3) ----------------------------
def test_first_time_sso_links_existing_email_to_one_identity(
    client, db, make_school, make_staff, monkeypatch
):
    _enable(monkeypatch, "microsoft")
    school = make_school(code="SCH-001")
    staff = make_staff(school, email="t@oakwood.edu", password="Password123")  # no sso_subject yet
    assert staff.sso_subject is None
    _mock_provider(monkeypatch, sub="ms-sub-9", email="t@oakwood.edu")

    # first SSO sign-in -> links the subject to the existing email account
    first = _callback(client, "microsoft", sso._sign_state("microsoft", school.id, "n1"))
    first_msg = _bridge_message(first)
    assert first_msg["ok"] is True
    sso_identity = decode_jwt(first_msg["access_token"])["sub"]

    db.refresh(staff)
    assert staff.sso_subject == "ms-sub-9"  # linked
    assert get_staff_by_sso_subject(db, "ms-sub-9", school.id).id == staff.id

    # second SSO sign-in resolves by subject to the SAME identity
    second = _callback(client, "microsoft", sso._sign_state("microsoft", school.id, "n2"))
    assert decode_jwt(_bridge_message(second)["access_token"])["sub"] == sso_identity == str(staff.id)

    # and password sign-in still resolves to that one identity
    pw = client.post(SIGNIN, json={"email": "t@oakwood.edu", "password": "Password123"})
    assert decode_jwt(pw.json()["access_token"])["sub"] == str(staff.id)


# ---- callback negatives: generic-failure bridge (ok:false, no token, no disclosure) -------------
# The popup needs to postMessage even on failure, so a failed callback is a 200 HTML bridge carrying
# {ok:false, error} — NOT a JSON 401 (a JSON body cannot run the bridge script). The error copy stays
# generic ("Sign-in failed") — no account-existence / tenant disclosure.
def _assert_failure_bridge(resp) -> dict:
    msg = _bridge_message(resp)
    assert msg["ok"] is False
    assert msg["type"] == "youhue-sso"
    assert msg["error"] == "Sign-in failed"  # generic — no disclosure
    assert "access_token" not in msg  # no token on the failure envelope
    assert _bridge_target_origin(resp) == sso.frontend_origin()  # still not "*"
    return msg


def test_sso_no_matching_account_is_generic_failure_bridge(client, make_school, monkeypatch):
    _enable(monkeypatch, "google")
    school = make_school(code="SCH-001")  # no staff at all
    _mock_provider(monkeypatch, sub="google-x", email="stranger@oakwood.edu")
    r = _callback(client, "google", sso._sign_state("google", school.id, "n1"))
    _assert_failure_bridge(r)


def test_sso_rejects_tampered_state(client, make_school, monkeypatch):
    _enable(monkeypatch, "google")
    make_school(code="SCH-001")
    _mock_provider(monkeypatch, sub="google-x", email="t@oakwood.edu")
    r = _callback(client, "google", "not-a-valid-signed-state")
    _assert_failure_bridge(r)


def test_sso_rejects_state_from_other_provider(client, make_school, monkeypatch):
    _enable(monkeypatch, "google")
    school = make_school(code="SCH-001")
    _mock_provider(monkeypatch, sub="google-x", email="t@oakwood.edu")
    # state minted for microsoft must not be usable on the google callback
    r = _callback(client, "google", sso._sign_state("microsoft", school.id, "n1"))
    _assert_failure_bridge(r)


def test_sso_rejects_unverified_email(client, make_school, make_staff, monkeypatch):
    _enable(monkeypatch, "google")
    school = make_school(code="SCH-001")
    make_staff(school, email="t@oakwood.edu", password="Password123")
    _mock_provider(monkeypatch, sub="google-x", email="t@oakwood.edu", email_verified=False)
    r = _callback(client, "google", sso._sign_state("google", school.id, "n1"))
    _assert_failure_bridge(r)


def test_sso_bridge_does_not_leak_id_token_or_client_secret(
    client, db, make_school, make_staff, monkeypatch
):
    """Only the server-issued session token belongs in the HTML; the provider id_token and the OAuth
    client secret must never appear in the bridge page."""
    _enable(monkeypatch, "google")
    school = make_school(code="SCH-001")
    staff = make_staff(school, email="head@oakwood.edu", password="Password123")
    staff.sso_subject = "google-sub-1"
    db.commit()
    _mock_provider(monkeypatch, sub="google-sub-1", email="head@oakwood.edu")

    r = _callback(client, "google", sso._sign_state("google", school.id, "n1"))
    assert "fake.jwt" not in r.text  # the mocked provider id_token
    assert "google-secret" not in r.text  # the OAuth client secret
    # the only token present is the intended session token
    assert decode_jwt(_bridge_message(r)["access_token"])["sub"] == str(staff.id)


def test_frontend_origin_fails_closed_when_unset(monkeypatch):
    """If frontend_base_url is unset/malformed the targetOrigin fails CLOSED to the opaque "null"
    origin (delivers to nothing) rather than the token-leaking wildcard "*"."""
    monkeypatch.setattr(sso.settings, "frontend_base_url", "")
    assert sso.frontend_origin() == "null"
    monkeypatch.setattr(sso.settings, "frontend_base_url", "http://localhost:5173/sign-in")
    assert sso.frontend_origin() == "http://localhost:5173"  # path stripped to a bare origin


def test_sso_bridge_escapes_script_breakout_in_embedded_values():
    """A value containing </script> or line separators must be neutralised so it cannot break out of
    the inline <script> — exercised directly on the bridge builder with a hostile error string."""
    resp = sso.callback_bridge_response(ok=False, error="</script><img src=x onerror=alert(1)>")
    html = resp.body.decode()
    assert "</script><img" not in html  # raw breakout sequence never emitted
    assert "\\u003c/script\\u003e" in html  # escaped instead
    # the escaped payload still round-trips to the original string for the FE listener
    envelope = json.loads(re.search(r"var message = (\{.*?\});", html).group(1))
    assert envelope["error"] == "</script><img src=x onerror=alert(1)>"


# ---- Scenario 5 (NEGATIVE): SSO-only account has NO local password reset -------------------------
def test_sso_only_account_has_no_local_reset(client, db, make_school, make_staff, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(staff_mod, "send_email", lambda to, subject, body: calls.append(to))
    school = make_school(code="SCH-001")
    staff = make_staff(school, email="sso@oakwood.edu", password=None)  # SSO-only (no password)
    staff.sso_subject = "google-only"
    db.commit()

    # forgot-password still returns 202 (no account-existence disclosure) ...
    r = client.post("/api/v1/auth/staff/forgot-password", json={"email": "sso@oakwood.edu"})
    assert r.status_code == 202
    # ... but NO reset link is issued and NO email is sent — the IdP owns the password.
    assert db.query(PasswordResetToken).count() == 0
    assert calls == []
