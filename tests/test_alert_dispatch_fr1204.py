"""FR-12-04: alert dispatch to configured adults (GATE G-7).

Wires FR-12-06's routing output through FR-12-05's recipient config into INFRA-05's existing
notification transport (POST /api/v1/alerts/dispatch) — reuses all three, owns none of them.
Also folds FR-12-10 (alert delivery retry — already INFRA-05 machinery, exercised here end-to-end).
"""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

import src.application.notifications.services as notif_mod
from src.application.notifications import services as notif
from src.application.risk import services as risk
from src.constants.enums import AlertChannel, DeliveryStatus, FlagBand, FlagEventType
from src.domain.billing.models import Notification
from src.domain.checkin.models import CheckIn
from src.domain.risk import services as risk_db
from src.domain.risk.models import AlertDelivery, FlagEvent

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str = "t@oakwood.edu") -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _boom(to: str, subject: str, body: str) -> None:
    raise RuntimeError("smtp down")


def _mk_checkin(db, student, school, reflection="i feel hopeless"):
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=1, reflection_text=reflection,
        submitted_at=datetime.now(UTC), local_date=datetime.now(UTC).date(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _immediate_flag(db, make_school, make_student):
    """A concern-word check-in, scored + routed to immediate band via FR-12-01/FR-12-06."""
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school)
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged and result.risk_score == 0.90
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    db.commit()
    return school, student, flag


def _deliveries(db, notification_id: uuid.UUID) -> dict[AlertChannel, AlertDelivery]:
    rows = db.scalars(
        select(AlertDelivery).where(AlertDelivery.notification_id == notification_id)
    ).all()
    return {r.channel: r for r in rows}


# ---- Scenario 1: a flag alerts the configured adults quickly (email + in-app) -----------------

def test_dispatch_alerts_configured_adults_email_and_in_app(
    db, make_school, make_student, make_staff
):
    school, student, flag = _immediate_flag(db, make_school, make_student)
    recipient = make_staff(school, email="lead@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    recipients = risk.dispatch_alert(db, flag)
    db.commit()

    assert recipients == 1
    n = db.query(Notification).filter(Notification.recipient_id == recipient.id).one()
    assert n.type == "risk_alert"
    d = _deliveries(db, n.id)
    assert d[AlertChannel.in_app].status == DeliveryStatus.delivered  # in-app: immediate
    assert d[AlertChannel.email].status == DeliveryStatus.queued  # email: queued for the worker
    events = db.query(FlagEvent).filter(FlagEvent.flag_id == flag.id).all()
    assert len(events) == 1 and events[0].event_type == FlagEventType.alerted


def test_dispatch_endpoint_202_and_within_a_minute_via_worker(
    client, db, make_school, make_student, make_staff
):
    school, student, flag = _immediate_flag(db, make_school, make_student)
    recipient = make_staff(school, email="lead2@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    r = client.post(
        "/api/v1/alerts/dispatch", json={"flag_id": str(flag.id)},
        headers=_auth(_staff_token(client, "lead2@oakwood.edu")),
    )
    assert r.status_code == 202
    assert r.json() == {"flag_id": str(flag.id), "recipients": 1}

    # in-app is delivered inline (no wait); email is queued and the worker pass sends it — the
    # DoD's "~1 minute" budget is the worker's queue latency, not a blocking send on this request.
    n = db.query(Notification).filter(Notification.recipient_id == recipient.id).one()
    assert _deliveries(db, n.id)[AlertChannel.in_app].status == DeliveryStatus.delivered
    processed = notif.process_due_deliveries(db)
    db.commit()
    assert processed == 1
    assert _deliveries(db, n.id)[AlertChannel.email].status == DeliveryStatus.sent


def test_dispatch_with_no_configured_route_is_not_an_error(db, make_school, make_student):
    # GATE G-5: no route configured yet -> zero recipients, not a 500
    school, student, flag = _immediate_flag(db, make_school, make_student)
    recipients = risk.dispatch_alert(db, flag)
    db.commit()
    assert recipients == 0
    assert db.query(Notification).count() == 0


# ---- Scenario 2 (NEG, GATE G-7): a delivery failure is surfaced, never dropped ----------------

def test_delivery_failure_after_retries_is_surfaced_and_logged_critical(
    db, make_school, make_student, make_staff, monkeypatch
):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    calls: list[str] = []
    monkeypatch.setattr(
        risk.logger, "critical", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    school, student, flag = _immediate_flag(db, make_school, make_student)
    recipient = make_staff(school, email="lead3@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    risk.dispatch_alert(db, flag)
    db.commit()
    n = db.query(Notification).filter(Notification.recipient_id == recipient.id).one()
    email_id = _deliveries(db, n.id)[AlertChannel.email].id

    for _ in range(notif.MAX_ATTEMPTS):  # simulate every backoff window elapsing
        d = db.get(AlertDelivery, email_id)
        d.next_attempt_at = datetime.now(UTC)
        db.commit()
        notif.process_due_deliveries(db)
        db.commit()

    email = db.get(AlertDelivery, email_id)
    assert email.status == DeliveryStatus.failed  # surfaced, not dropped
    assert email.attempts == notif.MAX_ATTEMPTS
    # not lost: the in-app copy is still visible even though email failed
    assert _deliveries(db, n.id)[AlertChannel.in_app].status == DeliveryStatus.delivered
    # GATE G-7: the ticket's own CRITICAL log, with flag_id, recipient_id, attempts
    matches = [c for c in calls if "fr_12_04_delivery_failed" in c]
    assert len(matches) == 1
    assert f"flag_id={flag.id}" in matches[0]
    assert f"recipient_id={recipient.id}" in matches[0]
    assert f"attempts={notif.MAX_ATTEMPTS}" in matches[0]


def test_non_flag_delivery_failure_does_not_emit_fr_12_04_log(
    db, make_school, make_staff, monkeypatch
):
    # a plain (non-alert) notification failing must never emit the alert-specific CRITICAL log
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    calls: list[str] = []
    monkeypatch.setattr(
        risk.logger, "critical", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    recipient = make_staff(make_school(), email="plain@oakwood.edu")
    n = notif.enqueue(db, recipient_id=recipient.id, ntype="invite", payload=None)
    db.commit()
    email_id = _deliveries(db, n.id)[AlertChannel.email].id
    for _ in range(notif.MAX_ATTEMPTS):
        d = db.get(AlertDelivery, email_id)
        d.next_attempt_at = datetime.now(UTC)
        db.commit()
        notif.process_due_deliveries(db)
        db.commit()
    assert db.get(AlertDelivery, email_id).status == DeliveryStatus.failed
    assert not any("fr_12_04_delivery_failed" in c for c in calls)


# ---- Recipients resolve ONLY from FR-12-05 config; dispatch is school-scoped -------------------

@pytest.mark.authz
def test_dispatch_endpoint_rejects_cross_school(client, db, make_school, make_staff, make_student):
    school_a = make_school(code="AAA-12", name="A12")
    make_staff(school_a, email="a12@oakwood.edu")
    school_b = make_school(code="BBB-12", name="B12")
    student_b = make_student(school_b)
    c = _mk_checkin(db, student_b, school_b)
    risk.score_checkin(db, c)
    db.commit()
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    db.commit()

    r = client.post(
        "/api/v1/alerts/dispatch", json={"flag_id": str(flag.id)},
        headers=_auth(_staff_token(client, "a12@oakwood.edu")),
    )
    assert r.status_code == 403


@pytest.mark.authz
def test_dispatch_endpoint_denies_student_session(client, db, make_school, make_student):
    school = make_school(code="OAK-12")
    student = make_student(school)
    c = _mk_checkin(db, student, school)
    risk.score_checkin(db, c)
    db.commit()
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    db.commit()
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": "OAK-12", "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.post("/api/v1/alerts/dispatch", json={"flag_id": str(flag.id)}, headers=_auth(token))
    assert r.status_code == 403


def test_dispatch_endpoint_unknown_flag_404(client, make_school, make_staff):
    make_staff(make_school())
    r = client.post(
        "/api/v1/alerts/dispatch", json={"flag_id": str(uuid.uuid4())},
        headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 404


def test_dispatch_endpoint_rejects_non_immediate_band(client, db, make_school, make_staff, make_student):
    school = make_school()
    make_staff(school)
    student = make_student(school)
    c = _mk_checkin(db, student, school)
    risk.score_checkin(db, c)
    db.commit()
    flag = risk_db.get_flag_by_checkin(db, c.id)  # band left null -- never routed to immediate
    r = client.post(
        "/api/v1/alerts/dispatch", json={"flag_id": str(flag.id)}, headers=_auth(_staff_token(client)),
    )
    assert r.status_code == 422


def test_recipients_never_invented_beyond_fr_12_05_config(db, make_school, make_student, make_staff):
    # a staff member who exists but is NOT in the FR-12-05 config never receives the alert
    school, student, flag = _immediate_flag(db, make_school, make_student)
    configured = make_staff(school, email="configured@oakwood.edu")
    make_staff(school, email="not-configured@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [configured.id])
    db.commit()
    risk.dispatch_alert(db, flag)
    db.commit()
    recipients = {n.recipient_id for n in db.query(Notification).all()}
    assert recipients == {configured.id}


# ---- Delivery is retried and each attempt's state is recorded (AlertDelivery.status) ----------

def test_delivery_retries_and_records_each_attempts_state(
    db, make_school, make_student, make_staff, monkeypatch
):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    school, student, flag = _immediate_flag(db, make_school, make_student)
    recipient = make_staff(school, email="lead4@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()
    risk.dispatch_alert(db, flag)
    db.commit()
    n = db.query(Notification).filter(Notification.recipient_id == recipient.id).one()
    email_id = _deliveries(db, n.id)[AlertChannel.email].id

    d = db.get(AlertDelivery, email_id)
    assert d.status == DeliveryStatus.queued and d.attempts == 0
    d.next_attempt_at = datetime.now(UTC)
    db.commit()
    notif.process_due_deliveries(db)
    db.commit()
    d = db.get(AlertDelivery, email_id)
    assert d.status == DeliveryStatus.retrying and d.attempts == 1  # each attempt recorded


# ---- idempotency: a retry of the dispatch endpoint never double-alerts -------------------------

def test_dispatch_is_idempotent_never_double_alerts(db, make_school, make_student, make_staff):
    school, student, flag = _immediate_flag(db, make_school, make_student)
    recipient = make_staff(school, email="lead5@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()

    first = risk.dispatch_alert(db, flag)
    db.commit()
    second = risk.dispatch_alert(db, flag)  # retry
    db.commit()

    assert first == 1
    assert second == 0
    assert db.query(Notification).count() == 1
    events = db.query(FlagEvent).filter(FlagEvent.flag_id == flag.id).all()
    assert len(events) == 1  # not re-recorded either
