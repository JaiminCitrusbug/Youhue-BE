"""Parental-consent capture + query (FR-20-06) — the COPPA verifiable-consent gate.

Verifiable parental consent is captured **school-mediated** (SC-088: "there is no parent-facing
screen" — the FERPA school-official exception): a `leadership` staff member attests consent on the
parent's behalf. Any other staff role, or a cross-school student, is denied (403, audited).

The consent-before-use gate (ticket §NEG / Must-nots): `require_verified_consent` is a REUSABLE
guard other tickets' data-use code paths call BEFORE proceeding — a `pending` (or absent) consent
refuses. FR-04-01 (the "associated data use" this consent gates) is a separate, not-yet-existing
ticket in this batch: nothing in this codebase calls the guard yet. It is shipped here so FR-04-01
can wire it in later; see the ticket report for the "awaiting a caller" note — do not read this as
already-enforced end-to-end.
"""
import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.application.authz import services as authz
from src.application.isolation import services as isolation
from src.constants.enums import ParentalConsentStatus, StaffRole
from src.domain.compliance import services as compliance_db
from src.domain.identity.models import StaffAccount, Student

logger = logging.getLogger("youhue.compliance")

# Only leadership records consent (SC-088 chrome role; leadership nav has a "Consent" item).
_CONSENT_ROLES = (StaffRole.leadership,)


def capture_consent(
    db: Session,
    staff: StaffAccount,
    student_id: uuid.UUID,
    requested_status: ParentalConsentStatus,
) -> ParentalConsentStatus:
    """Record (insert or update) the one consent row for a student. Returns the persisted status.
    Caller commits (mirrors the admin/concern-words pattern) so an audited denial still persists."""
    # INFRA-02 (which-rows): a cross-school or unknown id resolves to None -> existence is hidden.
    student = isolation.get_scoped(db, Student, student_id, staff.school_id)
    if student is None:
        logger.warning(
            "fr_20_06_forbidden staff=%s student=%s reason=not_found", staff.id, student_id
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    try:
        authz.require_roles(staff, *_CONSENT_ROLES)  # INFRA-03 (who-may)
    except HTTPException:
        logger.warning("fr_20_06_forbidden staff=%s student=%s reason=role", staff.id, student.id)
        isolation.audit(
            db, actor_id=staff.id, action="consent.capture.denied", target=str(student.id),
            school_id=staff.school_id,
        )
        raise

    try:
        row = compliance_db.upsert_consent(
            db, student_id=student.id, consent_status=requested_status
        )
    except Exception:
        logger.exception("fr_20_06_error staff=%s student=%s", staff.id, student.id)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "Could not record consent"
        ) from None

    isolation.audit(  # immutable audit of the sensitive action (ticket Baseline BR-12)
        db, actor_id=staff.id, action="consent.capture", target=str(student.id),
        school_id=staff.school_id,
    )
    logger.info(
        "fr_20_06_success staff=%s student=%s status=%s", staff.id, student.id, row.status.value
    )
    return row.status


def get_consent_status(db: Session, student_id: uuid.UUID) -> ParentalConsentStatus:
    """Current consent status for a student. No record yet -> `pending`: COPPA requires consent be
    captured before the associated use proceeds, so an absent record must never read as consent."""
    row = compliance_db.get_consent(db, student_id)
    return row.status if row is not None else ParentalConsentStatus.pending


def require_verified_consent(db: Session, student_id: uuid.UUID) -> None:
    """THE consent-before-use gate (ticket §NEG, Must-nots): raise 403 unless
    `consent_status == verified`. A `pending` (or missing) consent does not unlock the data use.

    This is the reusable dependency/guard a data-use code path calls first. It is NOT wired into any
    production endpoint by this ticket — see module docstring.
    """
    if get_consent_status(db, student_id) != ParentalConsentStatus.verified:
        logger.warning("fr_20_06_forbidden student=%s reason=consent_not_verified", student_id)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Verifiable parental consent required before this data use"
        )
