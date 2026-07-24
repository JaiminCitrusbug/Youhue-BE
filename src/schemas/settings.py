"""School leadership hub — the leadership-exposed settings surfaces (FR-16-02).

ONE hub endpoint (``PATCH /schools/{id}/settings``) applies exactly one changed setting per call —
``SchoolSettingsUpdate`` accepts three OPTIONAL, mutually-exclusive fields and a validator rejects a
body that sets zero or more than one (422). This ticket's job is to STORE and SURFACE these three
settings, never to build the engines that consume them:

  concern_words  — SC-060. The school's OVERRIDE on ``ConcernWordList`` (FR-19-05 owns the
                   platform default; FR-12-01 is the only consumer of the resolved list).
                   ``words`` here means this school's ADDITIONS on top of the (read-only, locked)
                   platform defaults — the approved screen's own framing ("platform defaults +
                   your additions", defaults "cannot be removed"). The persisted override row
                   itself is the FULL resolved list
                   (defaults ∪ additions), because ``resolve_words`` (application/risk/services.py)
                   treats a school override as a REPLACEMENT of the default, not an addend — see
                   ``application/leadership/services.py`` for how the two are reconciled.
  alert_routing  — SC-061. Ordered recipient chain per alert_type on ``AlertRecipientConfig``. This
                   ticket stores the config ONLY; the escalation/order ENGINE is FR-12-05.
  access_window  — SC-063. Start/end/timezone on ``CalendarConfig``. This ticket stores the config
                   ONLY; window ENFORCEMENT + calendar-period resolution is FR-07-03/FR-07-04.
"""
import uuid
from datetime import date, time
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ErrorResponse(BaseModel):
    """403 / 422 / 500 bodies — FastAPI's plain-string ``detail``."""

    detail: str


# --- write payloads (one of these three, never more than one) ----------------------------------


class ConcernWordsSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: list[str] = Field(default_factory=list)  # this school's ADDITIONS, not the full list


class AlertRoutingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alert_type: Literal["immediate", "triage"]
    recipient_staff_ids: list[uuid.UUID]


class AlertRoutingSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[AlertRoutingEntry]


class AccessWindowSetting(BaseModel):
    model_config = ConfigDict(extra="forbid")

    window_start: time
    window_end: time
    timezone: str
    # FR-07-04: minimal term-dates surface on the SAME setting/row FR-16-02 already owns. OPTIONAL
    # and paired — omit both to leave any previously-saved term dates untouched (this save is only
    # about window/timezone); provide both together to set/replace them. One-without-the-other is
    # rejected below (422), never a silent partial write.
    term_start: date | None = None
    term_end: date | None = None

    @model_validator(mode="after")
    def _term_dates_paired(self) -> Self:
        if (self.term_start is None) != (self.term_end is None):
            raise ValueError("term_start and term_end must be provided together")
        return self


class SchoolSettingsUpdate(BaseModel):
    """PATCH body. ``extra="forbid"`` (no field can escalate the write) + a validator that requires
    EXACTLY one of the three settings — a body naming zero or more than one is a 422, not a silent
    partial-apply or an ambiguous multi-setting write."""

    model_config = ConfigDict(extra="forbid")

    concern_words: ConcernWordsSetting | None = None
    alert_routing: AlertRoutingSetting | None = None
    access_window: AccessWindowSetting | None = None

    @model_validator(mode="after")
    def _exactly_one_setting(self) -> Self:
        provided = [
            v for v in (self.concern_words, self.alert_routing, self.access_window) if v is not None
        ]
        if len(provided) != 1:
            raise ValueError("Exactly one setting must be provided")
        return self


# --- read shapes (GET + the PATCH response echo the same snapshot) -----------------------------


class ConcernWordsOut(BaseModel):
    platform_defaults: list[str]  # read-only here — FR-19-05 owns authoring
    school_additions: list[str]


class AlertRoutingEntryOut(BaseModel):
    alert_type: str
    recipient_staff_ids: list[uuid.UUID]


class AccessWindowOut(BaseModel):
    window_start: time
    window_end: time
    timezone: str
    term_start: date | None = None  # FR-07-04 — null until leadership has ever saved a term
    term_end: date | None = None


class SchoolSettingsOut(BaseModel):
    concern_words: ConcernWordsOut
    alert_routing: list[AlertRoutingEntryOut]
    access_window: AccessWindowOut | None  # None until leadership has ever saved one


class SchoolSettingsResponse(BaseModel):
    settings: SchoolSettingsOut
