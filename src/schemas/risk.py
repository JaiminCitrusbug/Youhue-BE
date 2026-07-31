"""Risk-scoring request/response schemas (INFRA-06)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class ScoreRequest(BaseModel):
    checkin_id: uuid.UUID


class ScoreResponse(BaseModel):
    flagged: bool
    risk_score: float
    matched_terms: list[str]


class RouteRequest(BaseModel):
    """FR-12-06 — POST /api/v1/risk/route body."""

    checkin_id: uuid.UUID


class RouteResponse(BaseModel):
    band: str  # immediate | triage | none
    flag_id: uuid.UUID | None = None


class TriageQueueItem(BaseModel):
    flag_id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    type: str
    risk_score: float
    created_at: datetime


class TriageQueueResponse(BaseModel):
    flags: list[TriageQueueItem]


class SlowBurnEvaluateRequest(BaseModel):
    student_id: uuid.UUID


class SlowBurnEvaluateResponse(BaseModel):
    flag_raised: bool


class AlertDispatchRequest(BaseModel):
    """FR-12-04 — POST /api/v1/alerts/dispatch body."""

    flag_id: uuid.UUID


class AlertDispatchResponse(BaseModel):
    flag_id: uuid.UUID
    recipients: int


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


class AlertEscalateResponse(BaseModel):
    """FR-12-08 (GATE G-8) — POST /api/v1/alerts/{flagId}/escalate response."""

    flag_id: uuid.UUID
    escalated_to: uuid.UUID


class AlertAcknowledgeResponse(BaseModel):
    """FR-12-08 — POST /api/v1/alerts/{flagId}/acknowledge response. Structural minimum this
    ticket adds (no acknowledge endpoint pre-existed anywhere) so GATE G-8's negative rule — an
    acknowledged alert does not escalate — is testable end-to-end."""

    flag_id: uuid.UUID
    status: str


class FlagEventOut(BaseModel):
    """FR-12-09 — one row of a flag's immutable timeline."""

    type: str  # alerted | viewed | acted | escalated
    actor: str | None = None  # resolved staff email; null for a system-recorded event (alerted)
    at: datetime


class FlagEventsResponse(BaseModel):
    events: list[FlagEventOut]


class GuidanceLinkOut(BaseModel):
    """FR-13-04 — one advisory "useful link" entry. Label only (no live destination URL exists
    yet in this codebase); matches the approved GuidedResponse.tsx (SC-040) `links[].label` shape,
    the FE supplies its own icon."""

    label: str


class GuidanceOut(BaseModel):
    """FR-13-04 — GET /api/v1/flags/{id}/guidance. Advisory only (suggested wording, sensible
    next steps, useful links) a teacher may use, adapt or ignore when responding to a flagged
    check-in; nothing here is persisted, forced or auto-applied."""

    suggested_wording: str
    next_steps: list[str]
    links: list[GuidanceLinkOut]


class FlagStudentOut(BaseModel):
    """FR-13-05 — GET /api/v1/flags/{id}/student. The minimal identity a flag resolves to, so the
    guided-response screen's "send a private note" action can navigate to a real `student_id` and
    show a real name — deliberately NOT merged into `GuidanceOut` above (frozen by FR-13-04's own
    `test_guidance_is_advisory_only_no_gating_field` exact-keys assertion)."""

    student_id: uuid.UUID
    student_name: str
