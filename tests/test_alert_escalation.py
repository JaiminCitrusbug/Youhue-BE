"""FR-12-08: alert escalation on no acknowledgement (GATE G-8).

Escalates an alerted, unacknowledged flag to the NEXT adult in FR-12-05's ordered recipient list
via INFRA-05's transport — reuses FR-12-04 (dispatch), FR-12-05 (recipient order), INFRA-05
(transport), owns none of them. GATE G-8's whole point: an ACKNOWLEDGED alert never escalates.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import src.application.notifications.services as notif_mod
from config.env_config import settings
from src.application.notifications import services as notif
from src.application.risk import services as risk
from src.constants.enums import AlertChannel, DeliveryStatus, FlagBand, FlagEventType, FlagStatus
from src.domain.billing.models import Notification
from src.domain.checkin.models import CheckIn
from src.domain.risk import services as risk_db
from src.domain.risk.models import AlertDelivery, FlagEvent

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str) -> str:
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


def _alerted_flag(db, make_school, make_student, make_staff, n_recipients=2):
    """An immediate-band flag that FR-12-04 has already dispatched to `n_recipients` ordered
    configured adults — the precondition every escalation scenario in this file starts from."""
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school)
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    db.commit()
    recipients = [
        make_staff(school, email=f"r{i}-{uuid.uuid4().hex[:6]}@oakwood.edu")
        for i in range(n_recipients)
    ]
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [r.id for r in recipients])
    db.commit()
    risk.dispatch_alert(db, flag)
    db.commit()
    db.refresh(flag)
    return school, student, flag, recipients


def _alerted_flag_at(db, make_school, make_student, make_staff, alerted_at, n_recipients=2):
    """Like `_alerted_flag`, but the `alerted` FlagEvent's timestamp is set explicitly at INSERT
    time. `flag_events` is append-only (no UPDATE path exists — see its model docstring and the DB
    trigger `youhue_block_mutation()`), so backdating an event for the timeout tests below has to
    happen on creation, never via a later mutation of an already-written row."""
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school)
    result = risk.score_checkin(db, c)
    db.commit()
    assert result.flagged
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    recipients = [
        make_staff(school, email=f"r{i}-{uuid.uuid4().hex[:6]}@oakwood.edu")
        for i in range(n_recipients)
    ]
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [r.id for r in recipients])
    db.add(FlagEvent(flag_id=flag.id, event_type=FlagEventType.alerted, actor_id=None, at=alerted_at))
    db.commit()
    db.refresh(flag)
    return school, student, flag, recipients


def _deliveries(db, notification_id: uuid.UUID) -> dict[AlertChannel, AlertDelivery]:
    rows = db.scalars(
        select(AlertDelivery).where(AlertDelivery.notification_id == notification_id)
    ).all()
    return {r.channel: r for r in rows}


# ---- Scenario 1 (positive): an unacknowledged alert escalates to the next configured adult -----

def test_unacknowledged_alert_escalates_to_next_configured_adult(
    db, make_school, make_student, make_staff
):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 3)
    assert flag.status == FlagStatus.open  # real starting state, not acknowledged

    escalated_to = risk.escalate_alert(db, flag)
    db.commit()
    db.refresh(flag)

    assert escalated_to == recipients[1].id  # the NEXT adult in the ordered list, not arbitrary
    assert flag.status == FlagStatus.escalated  # real state transition, asserted directly
    events = db.query(FlagEvent).filter(
        FlagEvent.flag_id == flag.id, FlagEvent.event_type == FlagEventType.escalated
    ).all()
    assert len(events) == 1 and events[0].actor_id == recipients[1].id
    # recipients[1] also received the ORIGINAL dispatch (FR-12-04 alerts the whole configured
    # list at once) plus this escalation, so isolate by type rather than recipient alone.
    n = db.query(Notification).filter(
        Notification.recipient_id == recipients[1].id, Notification.type == "risk_alert_escalation"
    ).one()
    assert n.type == "risk_alert_escalation"


# ---- Scenario 2 (NEG, the gate): an acknowledged alert does NOT escalate -----------------------

def test_acknowledged_alert_does_not_escalate(db, make_school, make_student, make_staff):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    assert flag.status == FlagStatus.open  # before

    risk.acknowledge_alert(db, flag)
    db.commit()
    db.refresh(flag)
    assert flag.status == FlagStatus.acknowledged  # after — a REAL, different state than before

    with pytest.raises(HTTPException) as exc:
        risk.escalate_alert(db, flag)
    assert exc.value.status_code == 409

    db.rollback()
    db.refresh(flag)
    assert flag.status == FlagStatus.acknowledged  # unchanged — never silently escalated
    assert db.query(FlagEvent).filter(
        FlagEvent.flag_id == flag.id, FlagEvent.event_type == FlagEventType.escalated
    ).count() == 0
    assert db.query(Notification).filter(Notification.type == "risk_alert_escalation").count() == 0


# ---- Escalating an already-acknowledged alert is a conflict (edge, via the HTTP endpoint) -------

def test_escalate_endpoint_409_on_already_acknowledged(
    client, db, make_school, make_student, make_staff
):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    token = _auth(_staff_token(client, recipients[0].email))

    ack = client.post(f"/api/v1/alerts/{flag.id}/acknowledge", headers=token)
    assert ack.status_code == 200
    assert ack.json() == {"flag_id": str(flag.id), "status": "acknowledged"}

    r = client.post(f"/api/v1/alerts/{flag.id}/escalate", headers=token)
    assert r.status_code == 409


# ---- Escalation walks the ORDERED recipient list, not an arbitrary next adult -------------------

def test_escalation_targets_the_configured_order_not_an_arbitrary_adult(
    db, make_school, make_student, make_staff
):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 3)
    a, b, c = recipients
    escalated_to = risk.escalate_alert(db, flag)
    db.commit()
    assert escalated_to == b.id
    assert escalated_to != a.id
    assert escalated_to != c.id


def test_escalate_rejects_when_config_has_no_next_recipient(db, make_school, make_student, make_staff):
    # only ONE configured recipient -> nobody left to escalate to (422, not a 500 or a silent no-op)
    school, student, flag, _recipients = _alerted_flag(
        db, make_school, make_student, make_staff, 1
    )
    with pytest.raises(HTTPException) as exc:
        risk.escalate_alert(db, flag)
    assert exc.value.status_code == 422


def test_escalate_rejects_a_flag_that_was_never_alerted(db, make_school, make_student, make_staff):
    school = make_school()
    student = make_student(school)
    c = _mk_checkin(db, student, school)
    risk.score_checkin(db, c)
    db.commit()
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    db.commit()  # never dispatched -> no `alerted` FlagEvent
    with pytest.raises(HTTPException) as exc:
        risk.escalate_alert(db, flag)
    assert exc.value.status_code == 422


# ---- School-scoping (BR-01) ----------------------------------------------------------------------

@pytest.mark.authz
def test_escalate_endpoint_rejects_cross_school(client, db, make_school, make_staff, make_student):
    school_a = make_school(code="ESC-A1")
    make_staff(school_a, email="a@escalate.edu")
    school_b = make_school(code="ESC-B1")
    student_b = make_student(school_b)
    c = _mk_checkin(db, student_b, school_b)
    risk.score_checkin(db, c)
    db.commit()
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    recipients = [make_staff(school_b, email=f"rb{i}@escalate.edu") for i in range(2)]
    risk_db.set_alert_recipient_config(db, school_b.id, "immediate", [r.id for r in recipients])
    db.commit()
    risk.dispatch_alert(db, flag)
    db.commit()

    r = client.post(
        f"/api/v1/alerts/{flag.id}/escalate", headers=_auth(_staff_token(client, "a@escalate.edu")),
    )
    assert r.status_code == 403


@pytest.mark.authz
def test_escalate_endpoint_denies_student_session(client, db, make_school, make_student, make_staff):
    school, student, flag, _recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.post(f"/api/v1/alerts/{flag.id}/escalate", headers=_auth(token))
    assert r.status_code == 403


def test_escalate_endpoint_unknown_flag_404(client, make_school, make_staff):
    make_staff(make_school(), email="lonely@oakwood.edu")
    r = client.post(
        f"/api/v1/alerts/{uuid.uuid4()}/escalate",
        headers=_auth(_staff_token(client, "lonely@oakwood.edu")),
    )
    assert r.status_code == 404


# ---- Idempotency under a retry (ACID transaction, Baseline BR-05) --------------------------------

def test_escalate_is_idempotent_never_double_escalates(db, make_school, make_student, make_staff):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 3)

    first = risk.escalate_alert(db, flag)
    db.commit()
    second = risk.escalate_alert(db, flag)  # retry
    db.commit()

    assert first == second == recipients[1].id
    assert db.query(Notification).filter(Notification.type == "risk_alert_escalation").count() == 1
    events = db.query(FlagEvent).filter(
        FlagEvent.flag_id == flag.id, FlagEvent.event_type == FlagEventType.escalated
    ).all()
    assert len(events) == 1  # not re-recorded either


def test_escalate_endpoint_202_retry_returns_same_target(
    client, db, make_school, make_student, make_staff
):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    token = _auth(_staff_token(client, recipients[0].email))
    r1 = client.post(f"/api/v1/alerts/{flag.id}/escalate", headers=token)
    r2 = client.post(f"/api/v1/alerts/{flag.id}/escalate", headers=token)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json() == {
        "flag_id": str(flag.id), "escalated_to": str(recipients[1].id)
    }


# ---- The ack timeout and recipient order are configuration-driven (edge) -------------------------

def test_process_due_escalations_respects_the_not_yet_due_flag(
    db, make_school, make_student, make_staff
):
    # inside the configured window -> not due yet, not escalated
    _school, _student, flag, _r = _alerted_flag_at(
        db, make_school, make_student, make_staff,
        datetime.now(UTC) - timedelta(minutes=settings.alert_ack_timeout_minutes - 5),
    )
    assert risk.process_due_escalations(db) == 0
    db.refresh(flag)
    assert flag.status == FlagStatus.open


def test_process_due_escalations_escalates_the_due_flag(db, make_school, make_student, make_staff):
    # past the configured window -> due, escalates
    _school, _student, flag, _r = _alerted_flag_at(
        db, make_school, make_student, make_staff,
        datetime.now(UTC) - timedelta(minutes=settings.alert_ack_timeout_minutes + 5),
    )
    assert risk.process_due_escalations(db) == 1
    db.refresh(flag)
    assert flag.status == FlagStatus.escalated


def test_process_due_escalations_skips_acknowledged_flags(db, make_school, make_student, make_staff):
    _school, _student, flag, _r = _alerted_flag_at(
        db, make_school, make_student, make_staff,
        datetime.now(UTC) - timedelta(minutes=settings.alert_ack_timeout_minutes + 5),
    )
    risk.acknowledge_alert(db, flag)
    db.commit()

    assert risk.process_due_escalations(db) == 0  # GATE G-8: acknowledged -> never escalated
    db.refresh(flag)
    assert flag.status == FlagStatus.acknowledged


# ---- Structured log: fr_12_08_delivery_failed (delivery failure surfaced, not dropped) ----------

def test_escalation_delivery_failure_is_surfaced_and_logged_critical(
    db, make_school, make_student, make_staff, monkeypatch
):
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    calls: list[str] = []
    monkeypatch.setattr(
        risk.logger, "critical", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    risk.escalate_alert(db, flag)
    db.commit()
    # recipients[1] also has the ORIGINAL dispatch's notification (FR-12-04 alerts everyone at
    # once) — isolate the escalation one specifically by type.
    n = db.query(Notification).filter(
        Notification.recipient_id == recipients[1].id, Notification.type == "risk_alert_escalation"
    ).one()
    email_id = _deliveries(db, n.id)[AlertChannel.email].id

    for _ in range(notif.MAX_ATTEMPTS):
        d = db.get(AlertDelivery, email_id)
        d.next_attempt_at = datetime.now(UTC)
        db.commit()
        notif.process_due_deliveries(db)
        db.commit()

    email = db.get(AlertDelivery, email_id)
    assert email.status == DeliveryStatus.failed
    matches = [c for c in calls if "fr_12_08_delivery_failed" in c]
    assert len(matches) == 1
    assert f"flag_id={flag.id}" in matches[0]
    assert f"recipient_id={recipients[1].id}" in matches[0]
    assert f"attempts={notif.MAX_ATTEMPTS}" in matches[0]


def test_original_dispatch_delivery_failure_does_not_emit_fr_12_08_log(
    db, make_school, make_student, make_staff, monkeypatch
):
    # a delivery failure on the ORIGINAL dispatch (never escalated) must not emit the
    # escalation-owned log key — the two `_delivery_failed` keys stay distinct.
    monkeypatch.setattr(notif_mod, "send_email", _boom)
    calls: list[str] = []
    monkeypatch.setattr(
        risk.logger, "critical", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    n = db.query(Notification).filter(Notification.recipient_id == recipients[0].id).one()
    email_id = _deliveries(db, n.id)[AlertChannel.email].id
    for _ in range(notif.MAX_ATTEMPTS):
        d = db.get(AlertDelivery, email_id)
        d.next_attempt_at = datetime.now(UTC)
        db.commit()
        notif.process_due_deliveries(db)
        db.commit()
    assert db.get(AlertDelivery, email_id).status == DeliveryStatus.failed
    assert not any("fr_12_08_delivery_failed" in c for c in calls)
    assert any("fr_12_04_delivery_failed" in c for c in calls)


# ---- acknowledge (structural minimum) ------------------------------------------------------------

def test_acknowledge_endpoint_is_idempotent(client, db, make_school, make_student, make_staff):
    school, student, flag, recipients = _alerted_flag(db, make_school, make_student, make_staff, 2)
    token = _auth(_staff_token(client, recipients[0].email))
    r1 = client.post(f"/api/v1/alerts/{flag.id}/acknowledge", headers=token)
    r2 = client.post(f"/api/v1/alerts/{flag.id}/acknowledge", headers=token)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json() == {"flag_id": str(flag.id), "status": "acknowledged"}
