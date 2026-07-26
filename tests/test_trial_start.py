"""FR-17-03 — whole-school Premium trial starts on the school's first student check-in.

  Scenario 1  The first student check-in arms the trial clock (trial_start_at/trial_end_at set,
              6 weeks apart).                       -> test_first_checkin_starts_the_trial_clock
  Scenario 2  The whole school (not just the checking-in student) is on the SAME trial window.
              -> test_trial_window_is_whole_school_not_per_student
  Scenario 3  (NEG — GATE G-10) No check-in yet -> no running clock.
              -> test_no_checkin_yet_means_no_running_clock
  NEG         A second check-in (or a second explicit start) never re-arms an already-started
              clock.                                -> test_second_checkin_does_not_rearm_the_clock
                                                      -> test_endpoint_second_start_is_409
  Endpoint    POST /schools/{id}/trial/start: 200 on first call, 409 on a repeat, 403 cross-school.
              -> test_endpoint_starts_the_trial
              -> test_endpoint_forbidden_cross_school
  Concurrency Two students' genuinely-first check-ins race — only one clock start wins, the DB row
              lock serializes the rest (never two different trial_start_at values committed).
              -> test_concurrent_first_checkins_start_the_clock_exactly_once
"""
import threading
from datetime import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.constants.enums import (
    ParentalConsentStatus,
    SessionKind,
    StaffRole,
    SubscriptionState,
    SubscriptionTier,
)
from src.domain.billing.models import Subscription
from src.domain.compliance import services as compliance_db
from src.domain.identity.models import StaffAccount
from src.domain.org.models import CalendarConfig

CHECKINS = "/api/v1/check-ins"
TRIAL_START = "/api/v1/schools/{id}/trial/start"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint_staff(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _open_all_day_window(db, school, timezone: str = "UTC") -> CalendarConfig:
    row = CalendarConfig(
        school_id=school.id, window_start=time(0, 0), window_end=time(23, 59, 59),
        timezone=timezone,
    )
    db.add(row)
    db.commit()
    return row


def _verify_consent(db, student) -> None:
    compliance_db.upsert_consent(
        db, student_id=student.id, consent_status=ParentalConsentStatus.verified
    )
    db.commit()


def _student_token(client, school, student) -> str:
    r = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    return r.json()["session_token"]


def _ready(db, client, school, student) -> str:
    _open_all_day_window(db, school)
    _verify_consent(db, student)
    return _student_token(client, school, student)


def _arm(db, school) -> Subscription:
    """Mirrors FR-02-02's `arm_trial_subscription` effect: Premium trial armed, clock unstarted."""
    sub = Subscription(school_id=school.id, tier=SubscriptionTier.premium, state=SubscriptionState.trial)
    db.add(sub)
    db.commit()
    return sub


def test_no_checkin_yet_means_no_running_clock(db, make_school):
    school = make_school(code="TR-1")
    sub = _arm(db, school)
    assert sub.trial_start_at is None
    assert sub.trial_end_at is None


def test_first_checkin_starts_the_trial_clock(client, db, make_school, make_student):
    school = make_school(code="TR-2")
    _arm(db, school)
    student = make_student(school)
    token = _ready(db, client, school, student)

    r = client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    assert r.status_code == 201

    sub = db.query(Subscription).filter(Subscription.school_id == school.id).one()
    assert sub.trial_start_at is not None
    assert sub.trial_end_at is not None
    assert (sub.trial_end_at - sub.trial_start_at).days == 42  # 6 weeks


def test_trial_window_is_whole_school_not_per_student(client, db, make_school, make_student):
    """Scenario 2: the SAME Subscription row (one per school) carries the window — a second
    student at the same school does not get (or need) their own trial window."""
    school = make_school(code="TR-3")
    _arm(db, school)
    s1 = make_student(school, name="Amy")
    s2 = make_student(school, name="Ben")
    t1 = _ready(db, client, school, s1)

    client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(t1))
    sub = db.query(Subscription).filter(Subscription.school_id == school.id).one()
    first_start = sub.trial_start_at

    _verify_consent(db, s2)
    t2 = _student_token(client, school, s2)
    client.post(CHECKINS, json={"mood_value": 3}, headers=_auth(t2))

    db.refresh(sub)
    assert sub.trial_start_at == first_start  # unchanged — one whole-school window, not re-armed


def test_second_checkin_does_not_rearm_the_clock(client, db, make_school, make_student):
    school = make_school(code="TR-4")
    _arm(db, school)
    student = make_student(school)
    token = _ready(db, client, school, student)

    client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    sub = db.query(Subscription).filter(Subscription.school_id == school.id).one()
    first_start, first_end = sub.trial_start_at, sub.trial_end_at

    # A genuinely different day's check-in for the same student would need a fresh local_date,
    # but even the SAME day's duplicate-content retry (idempotent 201) must never touch the clock.
    client.post(CHECKINS, json={"mood_value": 4}, headers=_auth(token))
    db.refresh(sub)
    assert sub.trial_start_at == first_start
    assert sub.trial_end_at == first_end


def test_endpoint_starts_the_trial(client, db, make_school, make_staff):
    school = make_school(code="TR-5")
    _arm(db, school)
    staff = make_staff(school, email="t-tr5@school.edu", role=StaffRole.teacher)
    token = _mint_staff(db, staff)

    r = client.post(TRIAL_START.format(id=school.id), headers=_auth(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["trial_start_at"]
    assert body["trial_end_at"]


def test_endpoint_second_start_is_409(client, db, make_school, make_staff):
    school = make_school(code="TR-6")
    _arm(db, school)
    staff = make_staff(school, email="t-tr6@school.edu", role=StaffRole.teacher)
    token = _mint_staff(db, staff)

    first = client.post(TRIAL_START.format(id=school.id), headers=_auth(token))
    assert first.status_code == 200
    second = client.post(TRIAL_START.format(id=school.id), headers=_auth(token))
    assert second.status_code == 409


def test_endpoint_forbidden_cross_school(client, db, make_school, make_staff):
    school_a = make_school(code="TR-7A")
    school_b = make_school(code="TR-7B")
    _arm(db, school_b)
    staff = make_staff(school_a, email="t-tr7@school.edu", role=StaffRole.teacher)
    token = _mint_staff(db, staff)

    r = client.post(TRIAL_START.format(id=school_b.id), headers=_auth(token))
    assert r.status_code == 403


def test_concurrent_first_checkins_start_the_clock_exactly_once(db, make_school, make_student):
    """Real concurrency, real separate DB sessions (matches test_alert_config.py's established
    methodology). Worker A acquires the row lock first and holds it briefly (forces worker B to
    genuinely block on it, not just race for a head start); worker B's `now()` is captured well
    after A's, so a correct implementation must persist A's timestamp — never B's LATER one — no
    matter which worker's write reaches Postgres last. Without the lock, B's unlocked SELECT reads
    the stale pre-commit NULL, so B unconditionally writes too; Postgres serializes the two UPDATEs
    automatically (row-level locking is automatic even without FOR UPDATE) but does NOT re-check
    the application's "already armed" condition after unblocking — B's write silently wins,
    RE-ARMING the clock to a later timestamp (violates GATE G-10: once started, never restarted)."""
    import time as time_module

    from src.application.billing import services as billing_svc

    school = make_school(code="TR-8")
    _arm(db, school)
    db.commit()

    engine = create_engine(env_settings.database_url_test, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}
    recorded_start: dict[str, object] = {}

    def _worker_a() -> None:
        sess = Session()
        try:
            barrier.wait(timeout=30)
            sub = billing_svc.start_trial_on_first_checkin(sess, school.id)
            recorded_start["a"] = sub.trial_start_at if sub is not None else None
            time_module.sleep(0.5)  # hold the transaction open so B genuinely blocks/races on it
            sess.commit()
            results["a"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            sess.rollback()
            results["a"] = exc
        finally:
            sess.close()

    def _worker_b() -> None:
        sess = Session()
        try:
            barrier.wait(timeout=30)
            time_module.sleep(0.1)  # start just after A, well before A's 0.5s hold ends
            sub = billing_svc.start_trial_on_first_checkin(sess, school.id)
            recorded_start["b"] = sub.trial_start_at if sub is not None else None
            sess.commit()
            results["b"] = "ok"
        except BaseException as exc:  # noqa: BLE001
            sess.rollback()
            results["b"] = exc
        finally:
            sess.close()

    t1 = threading.Thread(target=_worker_a)
    t2 = threading.Thread(target=_worker_b)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not t1.is_alive() and not t2.is_alive()
    assert results.get("a") == "ok"
    assert results.get("b") == "ok"

    db.expire_all()
    sub = db.query(Subscription).filter(Subscription.school_id == school.id).one()
    assert sub.trial_start_at is not None
    assert (sub.trial_end_at - sub.trial_start_at).days == 42
    # The real proof: A's genuinely-first check-in armed the clock — B's later attempt must have
    # observed it already armed (returned None, never computed its own timestamp at all).
    assert recorded_start.get("a") is not None
    assert recorded_start.get("b") is None, "B must observe the clock already armed, not re-arm it"
