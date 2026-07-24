"""Parental-consent request/response schemas (FR-20-06). Minimum-data posture: no surplus fields —
only what the consent record needs (status), never parent PII (name/email/phone) the FE doesn't
collect either (SC-088 is school-mediated attestation, not a parent-facing form)."""
from pydantic import BaseModel

from src.constants.enums import ParentalConsentStatus


class ConsentIn(BaseModel):
    status: ParentalConsentStatus


class ConsentOut(BaseModel):
    status: ParentalConsentStatus
