"""FR-17-04 — automatic downgrade to Free at trial end / non-renewal / non-payment (GATE G-11),
and mid-term cancellation (no refund, access continues to term_end_at; folds FR-17-07).

  Scenario 1  A school drops to Free at trial end.
              -> test_trial_past_end_is_downgraded_to_free
              -> test_endpoint_downgrade_success
  Scenario 2  (NEG — GATE G-11) Premium data is retained read-only behind an upgrade prompt:
              downgrade never deletes the row, never clears trial/term history, and only
              withdraws Premium features (Free core keeps working).
              -> test_downgrade_retains_trial_history_never_deletes_the_row
              -> test_downgrade_only_withdraws_premium_features_free_core_keeps_working
  Scenario 3  Mid-term cancellation keeps access to the paid term end with no refund.
              -> test_mid_term_cancel_keeps_tier_and_term_end_at_unchanged
              -> test_cancelled_subscription_only_drops_to_free_after_term_end_at
  NEG         Downgrading an ineligible / already-Free subscription is a conflict (409).
              -> test_already_free_downgrade_is_409
              -> test_trial_not_yet_ended_is_409
              -> test_active_within_paid_term_is_409
              -> test_endpoint_repeat_downgrade_is_409
  Endpoint    403 for a role/session without `manage_accounts`; 409 for an ineligible cancel.
              -> test_endpoint_requires_admin_session
              -> test_cancel_without_term_end_at_is_409
  Sweep       `run_downgrade_sweep` downgrades every due subscription in one pass, skips the rest.
              -> test_sweep_downgrades_only_due_subscriptions
  Concurrency Two callers race a downgrade on the SAME already-due row — exactly one succeeds, the
              other observes the already-applied Free state and gets 409, never a lost update or a
              double-applied transition.
              -> test_concurrent_downgrade_applies_exactly_once
"""
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config.env_config import settings as env_settings
from src.application.billing import downgrade as downgrade_svc
from src.constants.enums import AdminRole, SubscriptionState, SubscriptionTier
from src.domain.billing.models import Subscription

ENDPOINT = "/api/v1/subscriptions"
SIGNIN = "/api/v1/admin/sign-in"


def _complete_signin(client, monkeypatch, email="admin@youhue.app", password="Password123"):
    import src.application.auth.admin as admin_mod

    captured: dict[str, str] = {}
    monkeypatch.setattr(
        admin_mod, "send_email", lambda to, subject, body: captured.update(body=body)
    )
    client.post(SIGNIN, json={"email": email, "password": password})
    code = captured["body"].split()[-1]
    return client.post(
        SIGNIN, json={"email": email, "password": password, "mfa_code": code}
    ).json()["admin_session"]


def _sub(db, school, **kwargs) -> Subscription:
    row = Subscription(school_id=school.id, **kwargs)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# ---- Scenario 1: trial end -> Free -------------------------------------------------------------


def test_trial_past_end_is_downgraded_to_free(db, make_school):
    school = make_school(code="DG-1")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)

    updated = downgrade_svc.downgrade_subscription(db, sub.id)
    db.commit()
    assert updated.tier == SubscriptionTier.free
    assert updated.state == SubscriptionState.free


def test_trial_not_yet_ended_is_409(db, make_school):
    school = make_school(code="DG-2")
    future = datetime.now(UTC) + timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=datetime.now(UTC), trial_end_at=future)

    with pytest.raises(HTTPException) as exc:
        downgrade_svc.downgrade_subscription(db, sub.id)
    assert exc.value.status_code == 409
    db.refresh(sub)
    assert sub.state == SubscriptionState.trial  # untouched


def test_active_within_paid_term_is_409(db, make_school):
    """Non-renewal only applies once the paid term has actually lapsed — an active subscription
    still inside its term is not eligible (no cancellation was ever recorded either)."""
    school = make_school(code="DG-3")
    future = datetime.now(UTC) + timedelta(days=30)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.active,
               term_end_at=future)

    with pytest.raises(HTTPException) as exc:
        downgrade_svc.downgrade_subscription(db, sub.id)
    assert exc.value.status_code == 409


def test_active_past_term_end_non_renewal_downgrades_to_free(db, make_school):
    school = make_school(code="DG-4")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.active,
               term_end_at=past)

    updated = downgrade_svc.downgrade_subscription(db, sub.id)
    db.commit()
    assert updated.state == SubscriptionState.free
    assert updated.tier == SubscriptionTier.free


def test_already_free_downgrade_is_409(db, make_school):
    school = make_school(code="DG-5")
    sub = _sub(db, school, tier=SubscriptionTier.free, state=SubscriptionState.free)

    with pytest.raises(HTTPException) as exc:
        downgrade_svc.downgrade_subscription(db, sub.id)
    assert exc.value.status_code == 409


def test_unknown_subscription_id_downgrade_is_409(db):
    import uuid

    with pytest.raises(HTTPException) as exc:
        downgrade_svc.downgrade_subscription(db, uuid.uuid4())
    assert exc.value.status_code == 409


# ---- Scenario 2 (NEG — GATE G-11): retained, read-only, only Premium withdrawn ------------------


def test_downgrade_retains_trial_history_never_deletes_the_row(db, make_school):
    school = make_school(code="DG-6")
    start = datetime.now(UTC) - timedelta(weeks=7)
    end = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=start, trial_end_at=end, trial_extension_count=1)

    downgrade_svc.downgrade_subscription(db, sub.id)
    db.commit()
    db.refresh(sub)
    # retained — not deleted, not cleared
    assert sub.trial_start_at == start
    assert sub.trial_end_at == end
    assert sub.trial_extension_count == 1
    still_there = db.scalar(select(Subscription).where(Subscription.id == sub.id))
    assert still_there is not None


def test_downgrade_only_withdraws_premium_features_free_core_keeps_working(
    client, db, make_school, make_staff
):
    """Scenario 1 + 2 together: after downgrade, GET /entitlements (FR-17-01, unmodified) proves
    nothing breaks — the Free core set is present and no Premium feature leaks onto the response."""
    from src.application.auth import sessions
    from src.constants.enums import SessionKind

    school = make_school(code="DG-7", name="Entitlements after downgrade")
    staff = make_staff(school, email="staff-dg7@school.edu")
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    token = sessions.issue_token(sess)

    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)
    downgrade_svc.downgrade_subscription(db, sub.id)
    db.commit()

    r = client.get(
        f"/api/v1/schools/{school.id}/entitlements", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "free"
    assert {"daily_checkins", "colleague_invites", "basic_mood_trends"} <= set(body["features"])
    assert "ai_analysis" not in body["features"]
    assert "custom_checkins" not in body["features"]


# ---- Scenario 3: mid-term cancellation ----------------------------------------------------------


def test_mid_term_cancel_keeps_tier_and_term_end_at_unchanged(db, make_school):
    school = make_school(code="DG-8")
    term_end = datetime.now(UTC) + timedelta(days=200)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.active,
               term_end_at=term_end)

    updated = downgrade_svc.cancel_subscription(db, sub.id)
    db.commit()
    assert updated.state == SubscriptionState.cancelled
    assert updated.tier == SubscriptionTier.premium  # access continues — no immediate downgrade
    assert updated.term_end_at == term_end  # unchanged — no refund, no shortened access


def test_cancelled_subscription_only_drops_to_free_after_term_end_at(db, make_school):
    school = make_school(code="DG-9")
    term_end = datetime.now(UTC) + timedelta(days=10)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.active,
               term_end_at=term_end)
    downgrade_svc.cancel_subscription(db, sub.id)
    db.commit()

    # still within the term — not yet eligible
    with pytest.raises(HTTPException) as exc:
        downgrade_svc.downgrade_subscription(db, sub.id)
    assert exc.value.status_code == 409
    db.refresh(sub)
    assert sub.tier == SubscriptionTier.premium

    # simulate the term actually ending
    sub.term_end_at = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    updated = downgrade_svc.downgrade_subscription(db, sub.id)
    db.commit()
    assert updated.state == SubscriptionState.free
    assert updated.tier == SubscriptionTier.free


def test_cancel_without_term_end_at_is_409(db, make_school):
    school = make_school(code="DG-10")
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.active)

    with pytest.raises(HTTPException) as exc:
        downgrade_svc.cancel_subscription(db, sub.id)
    assert exc.value.status_code == 409


def test_cancel_non_active_state_is_409(db, make_school):
    school = make_school(code="DG-11")
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_end_at=datetime.now(UTC) + timedelta(days=10))

    with pytest.raises(HTTPException) as exc:
        downgrade_svc.cancel_subscription(db, sub.id)
    assert exc.value.status_code == 409


# ---- Endpoint: admin-gated, structured status codes ----------------------------------------------


def test_endpoint_downgrade_success(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school(code="DG-12")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)
    token = _complete_signin(client, monkeypatch)

    r = client.post(
        f"{ENDPOINT}/{sub.id}/downgrade", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "free"


def test_endpoint_repeat_downgrade_is_409(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school(code="DG-13")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)
    token = _complete_signin(client, monkeypatch)
    auth = {"Authorization": f"Bearer {token}"}

    first = client.post(f"{ENDPOINT}/{sub.id}/downgrade", headers=auth)
    assert first.status_code == 200
    second = client.post(f"{ENDPOINT}/{sub.id}/downgrade", headers=auth)
    assert second.status_code == 409


def test_endpoint_requires_admin_session(client, db, make_school):
    school = make_school(code="DG-14")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)

    r = client.post(f"{ENDPOINT}/{sub.id}/downgrade")
    assert r.status_code == 403


@pytest.mark.authz
def test_endpoint_downgrade_denied_for_role_without_manage_accounts(
    client, db, make_admin, make_school, monkeypatch
):
    from src.application.authz import admin as admin_authz

    make_admin(email="support@youhue.app", role=AdminRole.support)
    school = make_school(code="DG-15")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)
    monkeypatch.setitem(admin_authz._ROLE_PERMISSIONS, AdminRole.support, frozenset())
    token = _complete_signin(client, monkeypatch, email="support@youhue.app")

    r = client.post(
        f"{ENDPOINT}/{sub.id}/downgrade", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


def test_endpoint_cancel_success(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school(code="DG-16")
    term_end = datetime.now(UTC) + timedelta(days=100)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.active,
               term_end_at=term_end)
    token = _complete_signin(client, monkeypatch)

    r = client.post(f"{ENDPOINT}/{sub.id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "cancelled"
    assert body["tier"] == "premium"


def test_endpoint_cancel_ineligible_is_409(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school(code="DG-17")
    sub = _sub(db, school, tier=SubscriptionTier.free, state=SubscriptionState.free)
    token = _complete_signin(client, monkeypatch)

    r = client.post(f"{ENDPOINT}/{sub.id}/cancel", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 409


# ---- Scheduled sweep ------------------------------------------------------------------------------


def test_sweep_downgrades_only_due_subscriptions(db, make_school):
    now = datetime.now(UTC)
    due_trial = _sub(db, make_school(code="DG-18"), tier=SubscriptionTier.premium,
                      state=SubscriptionState.trial, trial_start_at=now - timedelta(weeks=7),
                      trial_end_at=now - timedelta(days=1))
    not_due_trial = _sub(db, make_school(code="DG-19"), tier=SubscriptionTier.premium,
                          state=SubscriptionState.trial, trial_start_at=now,
                          trial_end_at=now + timedelta(weeks=6))
    due_cancelled = _sub(db, make_school(code="DG-20"), tier=SubscriptionTier.premium,
                          state=SubscriptionState.cancelled, term_end_at=now - timedelta(days=1))
    already_free = _sub(db, make_school(code="DG-21"), tier=SubscriptionTier.free,
                         state=SubscriptionState.free)

    downgraded_school_ids = downgrade_svc.run_downgrade_sweep(db)
    db.commit()

    assert due_trial.school_id in downgraded_school_ids
    assert due_cancelled.school_id in downgraded_school_ids
    assert not_due_trial.school_id not in downgraded_school_ids
    assert already_free.school_id not in downgraded_school_ids

    db.refresh(due_trial)
    db.refresh(not_due_trial)
    db.refresh(due_cancelled)
    assert due_trial.state == SubscriptionState.free
    assert not_due_trial.state == SubscriptionState.trial
    assert due_cancelled.state == SubscriptionState.free


def test_sweep_returns_empty_list_when_nothing_due(db, make_school):
    school = make_school(code="DG-22")
    _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
         trial_start_at=datetime.now(UTC), trial_end_at=datetime.now(UTC) + timedelta(weeks=6))

    assert downgrade_svc.run_downgrade_sweep(db) == []


# ---- Concurrency: two callers race a downgrade on the SAME due row ------------------------------


def test_concurrent_downgrade_applies_exactly_once(db, make_school):
    """Real concurrency, real separate DB sessions (matches test_trial_start.py's established
    methodology). Both threads target the SAME already-due Subscription row; the loser's
    `SELECT ... FOR UPDATE` blocks on the winner's uncommitted transition, then re-observes the
    row already Free under its own lock and correctly hits the 409 branch — never a lost update,
    never two 200s, never a double-applied transition."""
    school = make_school(code="DG-23")
    past = datetime.now(UTC) - timedelta(days=1)
    sub = _sub(db, school, tier=SubscriptionTier.premium, state=SubscriptionState.trial,
               trial_start_at=past - timedelta(weeks=6), trial_end_at=past)
    db.commit()

    engine = create_engine(env_settings.database_url_test, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    outcomes: dict[str, object] = {}

    def _worker_a() -> None:
        sess = Session()
        try:
            barrier.wait(timeout=30)
            downgrade_svc.downgrade_subscription(sess, sub.id)
            time.sleep(0.4)  # hold the row locked so B genuinely blocks on it, not just races
            sess.commit()
            outcomes["a"] = "ok"
        except HTTPException as exc:
            sess.rollback()
            outcomes["a"] = exc
        finally:
            sess.close()

    def _worker_b() -> None:
        sess = Session()
        try:
            barrier.wait(timeout=30)
            time.sleep(0.1)  # start just after A, well before A's hold ends
            downgrade_svc.downgrade_subscription(sess, sub.id)
            sess.commit()
            outcomes["b"] = "ok"
        except HTTPException as exc:
            sess.rollback()
            outcomes["b"] = exc
        finally:
            sess.close()

    t1 = threading.Thread(target=_worker_a)
    t2 = threading.Thread(target=_worker_b)
    t1.start()
    t2.start()
    t1.join(timeout=60)
    t2.join(timeout=60)

    assert not t1.is_alive() and not t2.is_alive()
    results = {outcomes.get("a"), outcomes.get("b")}
    assert "ok" in results, outcomes
    conflicts = [v for v in outcomes.values() if isinstance(v, HTTPException)]
    assert len(conflicts) == 1, outcomes
    assert conflicts[0].status_code == 409

    db.expire_all()
    final = db.scalar(select(Subscription).where(Subscription.id == sub.id))
    assert final is not None
    assert final.state == SubscriptionState.free
    assert final.tier == SubscriptionTier.free
    engine.dispose()
