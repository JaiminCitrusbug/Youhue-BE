"""FR-17-01: entitlements request/response schemas."""

from pydantic import BaseModel


class EntitlementsOut(BaseModel):
    tier: str
    features: list[str]
