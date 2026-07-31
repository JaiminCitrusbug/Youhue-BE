"""FR-12-09 — immutable flag-event timeline read (GET /api/v1/flags/{id}/events).

Covers @FR-12-09:
  Scenario 1 (positive): a flag has generated alerts to configured adults; an involved staff member
    opening the flag's record sees who was alerted and when (FR-12-04's existing `alerted` write).
  Scenario 2 (positive): adults have viewed and acted on a flag; an involved staff member sees who
    viewed, who acted, and when. The viewed/acted WRITE paths (FR-13-04/05) don't exist yet, so
    those rows are seeded directly here to prove the READ renders them correctly — this ticket owns
    the read + the `alerted` write only.
  Scenario 3 (NEG): read is restricted to the flag's own school's staff (403 otherwise) — this
    codebase has no narrower per-flag "involved staff" list anywhere in the data model, so
    "involved staff" is realized the same way every sibling /risk/* and /alerts/* endpoint realizes
    it: same-school staff (require_same_school). Cross-school staff get 403; an unknown flag 404s.
  Scenario 4 (NEG): the record is immutable — no PATCH/PUT/DELETE route exists on this resource
    (405), and the DB-level append-only trigger (migration c0ffee000001) blocks UPDATE/DELETE
    directly, same as `flag_events` already covered for FR-12-04 idempotency.
Plus: chronological ordering, actor resolution (staff email) vs. a system-recorded (actor_id null)
event, a student session denied, and the fr_12_09_success/_rejected/_forbidden/_error logs.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import DatabaseError

from src.application.risk import services as risk
from src.constants.enums import FlagBand, FlagEventType
from src.domain.checkin.models import CheckIn
from src.domain.risk import services as risk_db
from src.domain.risk.models import FlagEvent
from src.routers import flags as flags_router

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _staff_token(client, email: str = "t@oakwood.edu") -> str:
    return client.post(STAFF_SIGNIN, json={"email": email, "password": "Password123"}).json()[
        "access_token"
    ]


def _endpoint(flag_id: uuid.UUID) -> str:
    return f"/api/v1/flags/{flag_id}/events"


def _mk_checkin(db, student, school, reflection="i feel hopeless"):
    c = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=1, reflection_text=reflection,
        submitted_at=datetime.now(UTC), local_date=datetime.now(UTC).date(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _immediate_flag(db, school, student):
    """A concern-word check-in, scored + routed to immediate band (FR-12-01/FR-12-06)."""
    c = _mk_checkin(db, student, school)
    result = risk.score_checkin(db, c)
    assert result.flagged
    flag = risk_db.get_flag_by_checkin(db, c.id)
    risk_db.set_flag_band(db, flag, FlagBand.immediate)
    db.commit()
    return flag


# ---- Scenario 1: who was alerted, and when ------------------------------------------------------

def test_involved_staff_sees_who_was_alerted_and_when(
    db, client, make_school, make_student, make_staff, monkeypatch
):
    school = make_school()
    student = make_student(school)
    flag = _immediate_flag(db, school, student)
    recipient = make_staff(school, email="lead@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()
    risk.dispatch_alert(db, flag)
    db.commit()

    calls: list[str] = []
    monkeypatch.setattr(
        flags_router.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = client.get(_endpoint(flag.id), headers=_auth(_staff_token(client, "lead@oakwood.edu")))

    assert r.status_code == 200
    events = r.json()["events"]
    assert len(events) == 1
    assert events[0]["type"] == "alerted"
    assert events[0]["at"] is not None
    assert any("fr_12_09_success" in c and f"flag={flag.id}" in c for c in calls)


# ---- Scenario 2: who viewed and acted, and when -------------------------------------------------

def test_involved_staff_sees_who_viewed_and_acted_and_when(
    db, client, make_school, make_student, make_staff
):
    school = make_school()
    student = make_student(school)
    flag = _immediate_flag(db, school, student)
    viewer = make_staff(school, email="viewer@oakwood.edu")
    actor = make_staff(school, email="actor@oakwood.edu")
    t0 = datetime.now(UTC)
    db.add(FlagEvent(flag_id=flag.id, event_type=FlagEventType.viewed, actor_id=viewer.id, at=t0))
    db.add(
        FlagEvent(
            flag_id=flag.id, event_type=FlagEventType.acted, actor_id=actor.id,
            at=t0 + timedelta(minutes=2),
        )
    )
    db.commit()

    r = client.get(_endpoint(flag.id), headers=_auth(_staff_token(client, "viewer@oakwood.edu")))

    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["type"] for e in events] == ["viewed", "acted"]  # chronological, oldest first
    assert events[0]["actor"] == "viewer@oakwood.edu"
    assert events[1]["actor"] == "actor@oakwood.edu"


def test_full_timeline_orders_alerted_escalated_viewed_acted_chronologically(
    db, client, make_school, make_student, make_staff
):
    school = make_school()
    student = make_student(school)
    flag = _immediate_flag(db, school, student)
    recipient = make_staff(school, email="lead2@oakwood.edu")
    risk_db.set_alert_recipient_config(db, school.id, "immediate", [recipient.id])
    db.commit()
    risk.dispatch_alert(db, flag)  # alerted, actor_id=None (system-recorded)
    db.commit()
    pastoral = make_staff(school, email="pastoral@oakwood.edu")
    base = datetime.now(UTC) + timedelta(minutes=1)  # strictly after the alerted row above
    db.add(
        FlagEvent(
            flag_id=flag.id, event_type=FlagEventType.escalated, actor_id=pastoral.id,
            at=base + timedelta(minutes=5),
        )
    )
    db.add(
        FlagEvent(
            flag_id=flag.id, event_type=FlagEventType.viewed, actor_id=pastoral.id,
            at=base + timedelta(minutes=8),
        )
    )
    db.add(
        FlagEvent(
            flag_id=flag.id, event_type=FlagEventType.acted, actor_id=pastoral.id,
            at=base + timedelta(minutes=9),
        )
    )
    db.commit()

    r = client.get(_endpoint(flag.id), headers=_auth(_staff_token(client, "lead2@oakwood.edu")))

    assert r.status_code == 200
    events = r.json()["events"]
    assert [e["type"] for e in events] == ["alerted", "escalated", "viewed", "acted"]
    assert events[0]["actor"] is None  # system-recorded dispatch, no single human actor


# ---- NEG: restricted to the flag's own school's staff (403); unknown flag (404) -----------------

@pytest.mark.authz
def test_events_endpoint_rejects_cross_school(client, db, make_school, make_staff, make_student, monkeypatch):
    school_a = make_school(code="AAA-09", name="A09")
    make_staff(school_a, email="a09@oakwood.edu")
    school_b = make_school(code="BBB-09", name="B09")
    student_b = make_student(school_b)
    flag = _immediate_flag(db, school_b, student_b)

    calls: list[str] = []
    monkeypatch.setattr(
        flags_router.logger, "warning", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = client.get(_endpoint(flag.id), headers=_auth(_staff_token(client, "a09@oakwood.edu")))

    assert r.status_code == 403
    assert any("fr_12_09_forbidden" in c and "cross_tenant" in c for c in calls)


@pytest.mark.authz
def test_events_endpoint_denies_student_session(client, make_school, make_student):
    school = make_school(code="OAK-09")
    student = make_student(school)
    token = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": "OAK-09", "student_id": str(student.id)},
    ).json()["session_token"]
    r = client.get(_endpoint(uuid.uuid4()), headers=_auth(token))
    assert r.status_code == 403


def test_events_endpoint_unknown_flag_404(client, make_school, make_staff, monkeypatch):
    make_staff(make_school())
    calls: list[str] = []
    monkeypatch.setattr(
        flags_router.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = client.get(_endpoint(uuid.uuid4()), headers=_auth(_staff_token(client)))
    assert r.status_code == 404
    assert any("fr_12_09_rejected" in c and "not_found" in c for c in calls)


# ---- NEG: a 500 is surfaced, never silently dropped (Baseline BR-05) ----------------------------

def test_events_endpoint_surfaces_500_and_logs_error(
    db, client, make_school, make_student, make_staff, monkeypatch
):
    school = make_school()
    student = make_student(school)
    flag = _immediate_flag(db, school, student)
    make_staff(school)

    def _boom(*_a: object, **_k: object) -> list[FlagEvent]:
        raise RuntimeError("db went away")

    monkeypatch.setattr(flags_router.risk_db, "list_flag_events", _boom)
    calls: list[str] = []
    monkeypatch.setattr(
        flags_router.logger, "exception", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )

    r = client.get(_endpoint(flag.id), headers=_auth(_staff_token(client)))

    assert r.status_code == 500
    assert any("fr_12_09_error" in c and f"flag={flag.id}" in c for c in calls)


# ---- NEG: append-only / immutable ----------------------------------------------------------------

def test_no_write_route_exists_on_flag_events(db, client, make_school, make_student, make_staff):
    school = make_school()
    student = make_student(school)
    flag = _immediate_flag(db, school, student)
    make_staff(school)
    token = _staff_token(client)
    for method in ("post", "patch", "put", "delete"):
        r = getattr(client, method)(_endpoint(flag.id), headers=_auth(token))
        assert r.status_code == 405, f"{method.upper()} must not be a route on {_endpoint(flag.id)}"


def test_flag_events_cannot_be_mutated_at_the_db_level(db, make_school, make_student):
    school = make_school()
    student = make_student(school)
    flag = _immediate_flag(db, school, student)
    db.add(FlagEvent(flag_id=flag.id, event_type=FlagEventType.alerted, actor_id=None))
    db.commit()
    with pytest.raises(DatabaseError):  # append-only trigger (c0ffee000001) blocks UPDATE
        db.query(FlagEvent).update({FlagEvent.event_type: FlagEventType.acted})
        db.flush()
    db.rollback()
