"""Staff SSO (Google/Microsoft OAuth2/OIDC). A provider is live only when its creds are present.

`resolve_or_link` is the testable core: an SSO identity resolves to an existing StaffAccount,
and the first SSO matching an existing email links (both methods then resolve to one identity).
The HTTP authorize/callback dance uses Authlib and is exercised only when creds are configured.
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.config import settings
from src.domain.enums import StaffStatus
from src.infrastructure.models.identity import StaffAccount

PROVIDERS = ("google", "microsoft")


def is_enabled(provider: str) -> bool:
    if provider == "google":
        return bool(settings.google_client_id and settings.google_client_secret)
    if provider == "microsoft":
        return bool(settings.microsoft_client_id and settings.microsoft_client_secret)
    return False


def resolve_or_link(db: Session, subject: str, email: str, school_id: uuid.UUID) -> StaffAccount:
    """Resolve an SSO identity to a StaffAccount WITHIN the signing-in school, linking on first
    match to an existing email. Email is unique per school, so every lookup is school-scoped —
    an SSO identity can never resolve to (or link) an account in a different tenant.
    """
    staff = db.scalar(
        select(StaffAccount).where(
            StaffAccount.sso_subject == subject, StaffAccount.school_id == school_id
        )
    )
    if staff is not None:
        return staff
    staff = db.scalar(
        select(StaffAccount).where(
            StaffAccount.email == email.lower(),
            StaffAccount.school_id == school_id,
            StaffAccount.status == StaffStatus.active,
        )
    )
    if staff is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Sign-in failed")
    staff.sso_subject = subject  # link — password sign-in (if any) still works
    db.flush()
    return staff


def require_enabled(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown provider")
    if not is_enabled(provider):
        raise HTTPException(
            status.HTTP_501_NOT_IMPLEMENTED, f"SSO provider '{provider}' is not configured"
        )
