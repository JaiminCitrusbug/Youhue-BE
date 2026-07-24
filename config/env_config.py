"""Application settings — the single env contract for Youhue backend.

Pydantic v2 settings; every value is env-overridable (12-factor). Secrets never hard-coded:
JWT_SECRET falls back to a per-process random dev value, real value comes from env in any
deployed environment. External integrations use the ACTIVE_ENV TEST/ACTIVE dual-flow (backend.md):
  - ACTIVE_ENV=TEST   -> email writes .eml to EMAIL_FILE_DIR (local sink, no keys)
  - ACTIVE_ENV=ACTIVE -> email via SendGrid (needs SENDGRID_API_KEY)
  - MFA = email OTP only (leadership/district/admin)
  - Risk thresholds + default concern words are env-driven (D-05/D-18 pending ratification)
"""
import os
import secrets

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_jwt_secret() -> str:
    """Random per-process secret for local dev only; a deployed env MUST set JWT_SECRET."""
    if os.getenv("ENVIRONMENT", "local").lower() in {"staging", "production"}:
        raise ValueError("JWT_SECRET must be set explicitly in staging/production")
    return secrets.token_urlsafe(48)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # ---- app ----
    app_name: str = "Youhue — Student Wellbeing API"
    environment: str = "local"  # local | staging | production
    api_prefix: str = "/api/v1"

    # ---- database (owner provides DATABASE_URL in the active env; dev default = local Docker) ----
    database_url: str = "postgresql+psycopg://youhue:youhue@localhost:5433/youhue_dev"
    database_url_test: str = "postgresql+psycopg://youhue:youhue@localhost:5433/youhue_test"

    # ---- auth / session (INFRA-01) ----
    jwt_secret: str = Field(default_factory=_default_jwt_secret)
    jwt_algorithm: str = "HS256"
    staff_session_ttl_minutes: int = 720          # 12h staff working session
    student_session_ttl_minutes: int = 20         # short-lived, shared-device
    admin_session_ttl_minutes: int = 60           # internal admin console (MFA-gated, short)
    student_single_active_device: bool = True
    # account lockout
    lockout_max_attempts: int = 5
    lockout_window_minutes: int = 15
    # student passwordless sign-in — failed-attempt escalation (FR-01-02, M1 hardening).
    # Keyed on (client IP + presented code/token), so a bad actor hammering a class's SHARED code
    # can only throttle its OWN origin — never lock the whole class out (no DoS lever, no permanent
    # account lockout). A bounded cool-down window; refusal surfaces as 429 (rate-limit family).
    student_signin_max_attempts: int = 10
    student_signin_window_minutes: int = 15

    # ---- external integrations: TEST/ACTIVE dual flow (backend.md) ----
    active_env: str = "TEST"                       # TEST (local/mock) | ACTIVE (real providers)
    # email
    email_file_dir: str = "./var/mail"            # TEST sink (.eml files); git-ignored
    email_from: str = "no-reply@youhue.app"
    sendgrid_api_key: str | None = None           # required when ACTIVE_ENV=ACTIVE
    sendgrid_webhook_secret: str | None = None

    # ---- staff SSO (INFRA-01) — real OAuth 2.0/OIDC, enabled per-provider when creds present ----
    oauth_redirect_base: str = "http://localhost:8000"
    frontend_base_url: str = "http://localhost:5173"  # SPA base for user-facing links (reset)
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None

    # ---- MFA (INFRA-03) — email OTP only ----
    mfa_required_roles: str = "leadership,district,admin"
    mfa_otp_ttl_minutes: int = 10
    mfa_otp_length: int = 6
    mfa_max_attempts: int = 5  # OTP verification attempt cap (anti-brute-force)

    # ---- risk pipeline (INFRA-06) — env-driven; D-05/D-18 pending ratification ----
    # NB: this pipeline produces risk_score + Flag only; the action BAND is decided by FR-12-06
    # routing (ticket §Must-nots) — thresholds below are the score->triage cue routing will consume.
    risk_immediate_threshold: float = 0.80        # score cue: immediate concern
    risk_triage_threshold: float = 0.50           # score cue: triage, else no flag
    slowburn_low_mood_threshold: int = 2          # mood_value <= this counts as a "low" day
    slowburn_window_days: int = 5                 # trailing window the trend is evaluated over
    slowburn_min_low_days: int = 2                # distinct low days within the window to flag
    max_score_attempts: int = 3                   # retries before a scoring error dead-letters
    # platform default concern-word list (school lists override; PLACEHOLDER — needs ratification)
    default_concern_words: str = "hurt,scared,alone,hate,worthless,hopeless,unsafe,help"

    # ---- rate limiting (sign-in / token endpoints) ----
    rate_limit_signin_per_minute: int = 10
    # Public school self-registration (FR-02-01) gets its OWN, tighter bucket: an anonymous
    # internet-facing write must never be able to spend a legitimate teacher's sign-in budget.
    rate_limit_register_per_minute: int = 5
    # Enable ONLY where a reverse proxy rewrites X-Forwarded-For. If nothing strips the header a
    # caller can forge it and mint a fresh rate-limit bucket per request.
    trust_proxy_headers: bool = False

    # ---- roster CSV import (FR-03-01) ----
    roster_import_max_bytes: int = 2_000_000       # ~2MB — generous for a text CSV, still bounded
    roster_import_max_rows: int = 500              # mirrors the approved screen's stated cap

    # ---- daily check-in mood set (FR-04-01) — PLACEHOLDER pending client confirmation (ticket
    # Q-3: "the exact per-band mood set is an assumption pending client confirmation... carry the
    # set as configuration, do not hard-code contents as a product decision"). Same env-driven
    # placeholder posture as `default_concern_words` above. Values are indices into the fixed
    # 0..5 mood ladder `CheckIn.mood_value` already documents (0=most negative..5=most positive;
    # angry=0, sad=1, worried=2, ok=3, good=4, great=5 — matches the approved StudentCheckIn.tsx
    # MOODS array order). Each env var lists which of the six the age band sees, comma-separated;
    # the default reflects the approved screen's own copy ("younger pupils see fewer faces").
    mood_set_b5_7: str = "1,3,5"          # sad / ok / great — fewer faces for the youngest band
    mood_set_b8_11: str = "0,1,2,3,4,5"
    mood_set_b12_18: str = "0,1,2,3,4,5"

    @property
    def mfa_roles(self) -> list[str]:
        return [r.strip() for r in self.mfa_required_roles.split(",") if r.strip()]

    @property
    def concern_words(self) -> list[str]:
        return [w.strip().lower() for w in self.default_concern_words.split(",") if w.strip()]


settings = Settings()
