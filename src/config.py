"""Application settings — the single env contract for Youhue backend.

Pydantic v2 settings; every value is env-overridable (12-factor). Secrets never hard-coded:
JWT_SECRET falls back to a per-process random dev value, real value comes from env in any
deployed environment. Adapters (email, SSO, DB) are selected by env, per owner decisions:
  - EMAIL_BACKEND=file  -> writes .eml to EMAIL_FILE_DIR (local dev)
  - EMAIL_BACKEND=sendgrid -> SendGrid API (active env; needs SENDGRID_API_KEY)
  - MFA = email OTP only (leadership/district/admin)
  - Risk thresholds + default concern words are env-driven (D-05/D-18 pending ratification)
"""
import secrets

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(48))
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 30
    staff_session_ttl_minutes: int = 720          # 12h staff working session
    student_session_ttl_minutes: int = 20         # short-lived, shared-device
    student_single_active_device: bool = True
    # account lockout
    lockout_max_attempts: int = 5
    lockout_window_minutes: int = 15

    # ---- email transport (INFRA-05) ----
    email_backend: str = "file"                   # file | sendgrid
    email_file_dir: str = "./var/mail"            # where file backend drops .eml
    email_from: str = "no-reply@youhue.app"
    sendgrid_api_key: str | None = None           # required only when email_backend=sendgrid
    sendgrid_webhook_secret: str | None = None

    # ---- staff SSO (INFRA-01) — real OAuth 2.0/OIDC, enabled per-provider when creds present ----
    oauth_redirect_base: str = "http://localhost:8000"
    google_client_id: str | None = None
    google_client_secret: str | None = None
    microsoft_client_id: str | None = None
    microsoft_client_secret: str | None = None

    # ---- MFA (INFRA-03) — email OTP only ----
    mfa_required_roles: str = "leadership,district,admin"
    mfa_otp_ttl_minutes: int = 10
    mfa_otp_length: int = 6

    # ---- risk pipeline (INFRA-06) — env-driven; D-05/D-18 pending ratification ----
    risk_immediate_threshold: float = 0.80        # >= immediate band
    risk_triage_threshold: float = 0.50           # >= triage band, else none
    slowburn_low_mood_threshold: int = 2          # mood_value <= this counts as low
    slowburn_window_days: int = 5                 # consecutive-day window
    # platform default concern-word list (school lists override; PLACEHOLDER — needs ratification)
    default_concern_words: str = "hurt,scared,alone,hate,worthless,hopeless,unsafe,help"

    # ---- rate limiting (sign-in / token endpoints) ----
    rate_limit_signin_per_minute: int = 10

    @property
    def mfa_roles(self) -> list[str]:
        return [r.strip() for r in self.mfa_required_roles.split(",") if r.strip()]

    @property
    def concern_words(self) -> list[str]:
        return [w.strip().lower() for w in self.default_concern_words.split(",") if w.strip()]


settings = Settings()
