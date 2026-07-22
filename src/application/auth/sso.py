"""Staff SSO (Google/Microsoft OAuth2/OIDC) business logic. A provider is live only when its creds
are present. `resolve_or_link` resolves an SSO identity to a StaffAccount WITHIN the signing-in
school, linking on first match to an existing email. DB access via domain services.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from config.env_config import settings
from src.domain.identity import services as identity_db
from src.domain.identity.models import StaffAccount

PROVIDERS = ("google", "microsoft")


def is_enabled(provider: str) -> bool:
    if provider == "google":
        return bool(settings.google_client_id and settings.google_client_secret)
    if provider == "microsoft":
        return bool(settings.microsoft_client_id and settings.microsoft_client_secret)
    return False


def resolve_or_link(db: Session, subject: str, email: str, school_id: uuid.UUID) -> StaffAccount:
    """Email is unique per school, so every lookup is school-scoped — an SSO identity can never
    resolve to (or link) an account in a different tenant."""
    staff = identity_db.get_staff_by_sso_subject(db, subject, school_id)
    if staff is not None:
        return staff
    staff = identity_db.get_active_staff_by_email_in_school(db, email, school_id)
    if staff is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")
    identity_db.link_sso_subject(db, staff, subject)  # link — password sign-in (if any) still works
    return staff


def require_enabled(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown provider")
    if not is_enabled(provider):
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED, f"SSO provider '{provider}' is not configured"
        )
