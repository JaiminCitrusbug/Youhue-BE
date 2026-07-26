"""FR-12-06: route each scored check-in to exactly one band (immediate | triage | none).
GATE G-9 — on routing completion the system surfaces the flag for a human and takes NO other
action on the student. Folds FR-12-07 (band cut-offs env-configurable, bias toward triage) and
FR-12-11 (no automated action) — both verified here, not as separate tickets."""
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest

from config.env_config import settings
from src.application.risk import services as risk
from src.constants.enums import FlagBand, FlagEventType, ParentalConsentStatus
from src.domain.billing.models import Notification
from src.domain.checkin.models import CheckIn
from src.domain.compliance import services as compliance_db
from src.domain.identity.models import Student
from src.domain.org.models import CalendarConfig
from src.domain.risk import services as risk_db
from src.domain.risk.models import Flag, FlagEvent

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str = "t@oakwood.edu") -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _mk_checkin(db, student, school, mood=3, reflection=None, when=None):
    at = when or datetime.now(UTC)
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=mood,
        reflection_text=reflection, submitted_at=at, local_date=at.date(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _flagged_immediate(db, make_school, make_student):
    """A concern-word check-in -> 0.90 risk_score -> immediate band once routed."""
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel hopeless")
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged and result.risk_score == 0.90
    return school, student, c


def _flagged_triage(db, make_school, make_student):
    """A slow-burn streak -> 0.70 risk_score -> triage band once routed."""
    school = make_school()
    student = make_student(school)
    for i in range(1, settings.slowburn_window_days):
        _mk_checkin(db, student, school, mood=1, when=datetime.now(UTC) - timedelta(days=i))
    latest = _mk_checkin(db, student, school, mood=1)
    result = risk.score_checkin(db, latest)
    db.commit()
    assert result.flagged and result.risk_score == 0.70
    return school, student, latest


# ---- band decision -----------------------------------------------------------

def test_decide_band_uses_configurable_thresholds(monkeypatch):
    assert risk.decide_band(0.90) == FlagBand.immediate
    assert risk.decide_band(0.70) == FlagBand.triage
    assert risk.decide_band(0.10) == FlagBand.none
    # env-configurable, never hard-coded (ticket §Must-nots) — moving the cut-off moves the decision
    monkeypatch.setattr(settings, "risk_immediate_threshold", 0.95)
    assert risk.decide_band(0.90) == FlagBand.triage


def test_ambiguous_score_biases_toward_triage_not_silence():
    # a score sitting exactly on the triage floor lands in triage (human review), never falls
    # through to none — "default to erring toward triage over silence" (ticket §Must-nots)
    assert risk.decide_band(settings.risk_triage_threshold) == FlagBand.triage


# ---- Scenario 1: immediate band alerts configured adults ---------------------

def test_immediate_band_routes_and_alerts_configured_adults(
    db, make_school, make_student, make_staff
):
    school, student, c = _flagged_immediate(db, make_school, make_student)
    recipient = make_staff(school, email="lead@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    band, flag_id = risk.route_checkin(db, c.id)
    db.commit()

    assert band == FlagBand.immediate
    flag = db.get(Flag, flag_id)
    assert flag.band == FlagBand.immediate
    notifications = db.query(Notification).filter(Notification.recipient_id == recipient.id).all()
    assert len(notifications) == 1 and notifications[0].type == "risk_alert"
    events = db.query(FlagEvent).filter(FlagEvent.flag_id == flag_id).all()
    assert len(events) == 1 and events[0].event_type == FlagEventType.alerted


def test_immediate_band_with_no_configured_route_is_not_an_error(db, make_school, make_student):
    # GATE G-5 (FR-12-05 docstring): no route configured yet -> zero recipients, not a 500
    school, student, c = _flagged_immediate(db, make_school, make_student)
    band, flag_id = risk.route_checkin(db, c.id)
    db.commit()
    assert band == FlagBand.immediate
    assert db.query(Notification).count() == 0


# ---- Scenario 2: triage band queues for review, no immediate alert -----------

def test_triage_band_routes_to_queue_without_immediate_alert(
    db, make_school, make_student, make_staff
):
    school, student, c = _flagged_triage(db, make_school, make_student)
    recipient = make_staff(school, email="lead2@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    band, flag_id = risk.route_checkin(db, c.id)
    db.commit()

    assert band == FlagBand.triage
    assert db.query(Notification).count() == 0  # no immediate alert for a triage-band flag
    queued = risk_db.list_open_triage_flags(db, school.id)
    assert len(queued) == 1 and queued[0].id == flag_id


def test_no_flag_checkin_routes_to_none(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school, mood=5, reflection="great day")
    risk.score_checkin(db, c)  # clean -> no Flag row created
    db.commit()
    band, flag_id = risk.route_checkin(db, c.id)
    assert band == FlagBand.none and flag_id is None


# ---- Scenario 3 (GATE G-9, NEG): no automated action on the student ----------

def test_routing_takes_no_automated_action_on_the_student(
    db, make_school, make_student, make_staff
):
    school, student, c = _flagged_immediate(db, make_school, make_student)
    recipient = make_staff(school, email="lead3@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()
    before = Student.__table__
    before_row = dict(db.execute(before.select().where(before.c.id == student.id)).mappings().one())

    risk.route_checkin(db, c.id)
    db.commit()

    after_row = dict(db.execute(before.select().where(before.c.id == student.id)).mappings().one())
    assert before_row == after_row  # the student row itself is untouched by routing
    # every notification this routing produced went to STAFF, never the student
    notifications = db.query(Notification).all()
    assert all(n.recipient_id != student.id for n in notifications)
    assert all(n.recipient_id == recipient.id for n in notifications)


# ---- idempotency ---------------------------------------------------------------

def test_routing_is_idempotent_never_double_alerts(db, make_school, make_student, make_staff):
    school, student, c = _flagged_immediate(db, make_school, make_student)
    recipient = make_staff(school, email="lead4@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    first_band, first_flag_id = risk.route_checkin(db, c.id)
    db.commit()
    second_band, second_flag_id = risk.route_checkin(db, c.id)  # retry
    db.commit()

    assert first_band == second_band == FlagBand.immediate
    assert first_flag_id == second_flag_id
    assert db.query(Notification).count() == 1  # not re-alerted on retry


# ---- POST /api/v1/risk/route --------------------------------------------------

def test_route_endpoint_immediate(client, db, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school)
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel hopeless")
    risk.score_checkin(db, c)
    db.commit()
    r = client.post(
        "/api/v1/risk/route", json={"checkin_id": str(c.id)}, headers=_auth(_staff_token(client))
    )
    assert r.status_code == 200
    assert r.json()["band"] == "immediate"


@pytest.mark.authz
def test_route_endpoint_rejects_cross_school(client, db, make_school, make_staff, make_student):
    school_a = make_school(code="AAA-9", name="A9")
    make_staff(school_a, email="a9@oakwood.edu")
    school_b = make_school(code="BBB-9", name="B9")
    student_b = make_student(school_b)
    c = _mk_checkin(db, student_b, school_b, reflection="i feel unsafe")
    risk.score_checkin(db, c)
    db.commit()
    r = client.post(
        "/api/v1/risk/route", json={"checkin_id": str(c.id)},
        headers=_auth(_staff_token(client, "a9@oakwood.edu")),
    )
    assert r.status_code == 403


def test_route_endpoint_unknown_checkin_404(client, make_school, make_staff):
    school = make_school()
    make_staff(school)
    r = client.post(
        "/api/v1/risk/route", json={"checkin_id": str(uuid.uuid4())},
        headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 404


@pytest.mark.authz
def test_route_endpoint_denies_student_session(client, db, make_school, make_student):
    school = make_school(code="OAK-7")
    student = make_student(school)
    c = _mk_checkin(db, student, school, reflection="i feel unsafe")
    risk.score_checkin(db, c)
    db.commit()
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": "OAK-7", "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.post("/api/v1/risk/route", json={"checkin_id": str(c.id)}, headers=_auth(token))
    assert r.status_code == 403


# ---- GET /api/v1/risk/triage (SC-038) ------------------------------------------

def test_triage_queue_endpoint_lists_only_own_school_open_triage_flags(
    client, db, make_school, make_staff, make_student
):
    school_a = make_school(code="AAA-8", name="A8")
    make_staff(school_a, email="a8@oakwood.edu")
    school_b = make_school(code="BBB-8", name="B8")
    make_staff(school_b, email="b8@oakwood.edu")

    _, student_a, c_a = _flagged_triage(db, lambda **_: school_a, make_student)
    band_a, flag_a = risk.route_checkin(db, c_a.id)
    db.commit()
    assert band_a == FlagBand.triage

    _, student_b, c_b = _flagged_triage(db, lambda **_: school_b, make_student)
    risk.route_checkin(db, c_b.id)
    db.commit()

    r = client.get("/api/v1/risk/triage", headers=_auth(_staff_token(client, "a8@oakwood.edu")))
    assert r.status_code == 200
    flags = r.json()["flags"]
    assert len(flags) == 1
    assert flags[0]["flag_id"] == str(flag_a)
    assert flags[0]["student_id"] == str(student_a.id)
    assert flags[0]["student_name"] == student_a.display_name


def test_triage_queue_empty_state(client, make_school, make_staff):
    school = make_school()
    make_staff(school)
    r = client.get("/api/v1/risk/triage", headers=_auth(_staff_token(client)))
    assert r.status_code == 200 and r.json() == {"flags": []}


# ---- integration: check-in submit auto-routes (mirrors FR-12-01's DEF-005 closure) ------------

def test_checkin_submit_auto_routes_immediate_and_alerts(
    client, db, make_school, make_staff, make_student
):
    school = make_school(code="OAK-6")
    recipient = make_staff(school, email="lead6@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()
    student = make_student(school)
    # wire the two prerequisite guards open (window + consent, FR-07-03/FR-20-06) — same pattern
    # as test_checkin_submit.py's `_ready` helper — so submit reaches the write path at all.
    db.add(CalendarConfig(
        school_id=school.id, window_start=time(0, 0), window_end=time(23, 59, 59), timezone="UTC",
    ))
    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.verified
    )
    db.commit()
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": "OAK-6", "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.post(
        "/api/v1/check-ins", json={"mood_value": 1, "reflection_text": "i feel hopeless"},
        headers=_auth(token),
    )
    assert r.status_code == 201
    flag = db.query(Flag).filter(Flag.student_id == student.id).one()
    assert flag.band == FlagBand.immediate  # routed automatically, same call chain as scoring
    assert db.query(Notification).filter(Notification.recipient_id == recipient.id).count() == 1
