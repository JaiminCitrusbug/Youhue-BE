"""Risk-scoring request/response schemas (INFRA-06)."""
import uuid

from pydantic import BaseModel


class ScoreRequest(BaseModel):
    checkin_id: uuid.UUID


class ScoreResponse(BaseModel):
    flagged: bool
    risk_score: float
    matched_terms: list[str]
