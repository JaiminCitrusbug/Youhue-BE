"""Risk-scoring request/response schemas (INFRA-06)."""
import uuid

from pydantic import BaseModel, Field


class ScoreRequest(BaseModel):
    checkin_id: uuid.UUID


class ScoreResponse(BaseModel):
    flagged: bool
    risk_score: float
    matched_terms: list[str]


class SlowBurnEvaluateRequest(BaseModel):
    student_id: uuid.UUID


class SlowBurnEvaluateResponse(BaseModel):
    flag_raised: bool


class AlertConfigRequest(BaseModel):
    """FR-12-05: PUT /api/v1/schools/{id}/alert-config body — one alert_type's ordered recipient
    chain per call (distinct from FR-16-02's settings PATCH, which batches every alert_type in one
    `routes: [...]` payload)."""

    alert_type: str
    recipient_staff_ids: list[uuid.UUID] = Field(min_length=1)


class AlertConfigOut(BaseModel):
    alert_type: str
    recipient_staff_ids: list[uuid.UUID]


class AlertConfigResponse(BaseModel):
    config: AlertConfigOut
