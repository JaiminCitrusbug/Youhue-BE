"""FR-19-07 — admin-console platform statistics (SC-074): schools, active trials, check-in volume,
alert volume — read-only aggregates, `view_statistics`-gated, empty-state before any data.

Covers @FR-19-07:
  Scenario 1 (positive): an internal team member sees counts of schools/active trials/check-in
    volume/alert volume.
  Scenario 2 (NEG — empty state, not an error): no activity yet -> all-zero counts, 200, not 500.
Plus: RBAC deny-cell (403, audit-logged) for a role without `view_statistics`, figures render
platform-wide (no per-school/child data), active_trials correctly excludes an armed-but-not-started
trial (GATE G-10).
"""
from datetime import UTC, datetime, timedelta

import src.application.auth.admin as admin_mod
from src.constants.enums import AdminRole, FlagType, SubscriptionState
from src.domain.billing.models import Subscription
from src.domain.checkin.models import CheckIn
from src.domain.compliance.models import AuditLog
from src.domain.risk.models import Flag

ENDPOINT = "/api/v1/admin/stats"
SIGNIN = "/api/v1/admin/sign-in"


def _complete_signin(client, monkeypatch, email="admin@youhue.app", password="Password123"):
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        admin_mod, "send_email", lambda to, subject, body: captured.update(body=body)
    )
    client.post(SIGNIN, json={"email": email, "password": password})
    code = captured["body"].split()[-1]
    return client.post(
        SIGNIN, json={"email": email, "password": password, "mfa_code": code}
    ).json()["admin_session"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---- Scenario 2 (NEG): empty state before any data --------------------------------------------

def test_empty_platform_returns_all_zero_counts_not_an_error(client, db, make_admin, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {
        "schools": 0, "active_trials": 0, "checkin_volume": 0, "alert_volume": 0,
    }


# ---- Scenario 1 (positive): real counts --------------------------------------------------------

def test_stats_reflect_real_platform_activity(
    client, db, make_admin, make_school, make_student, monkeypatch
):
    make_admin(role=AdminRole.superadmin)
    school_a = make_school(code="AAA-7")
    school_b = make_school(code="BBB-7")
    student = make_student(school_a)

    # an ARMED-and-RUNNING trial counts (GATE G-10: start recorded, not yet expired)
    now = datetime.now(UTC)
    db.add(Subscription(
        school_id=school_a.id, state=SubscriptionState.trial,
        trial_start_at=now - timedelta(days=1), trial_end_at=now + timedelta(weeks=5),
    ))
    # an ARMED-but-NOT-STARTED trial does NOT count (no first check-in yet)
    db.add(Subscription(school_id=school_b.id, state=SubscriptionState.trial))
    db.add(CheckIn(
        student_id=student.id, school_id=school_a.id, mood_value=3,
        submitted_at=now, local_date=now.date(),
    ))
    db.add(CheckIn(
        student_id=student.id, school_id=school_a.id, mood_value=2,
        submitted_at=now - timedelta(days=1), local_date=(now - timedelta(days=1)).date(),
    ))
    db.add(Flag(
        student_id=student.id, school_id=school_a.id, type=FlagType.concern_word, risk_score=0.90,
    ))
    db.commit()

    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["schools"] == 2
    assert body["active_trials"] == 1  # only school_a's armed-and-running trial
    assert body["checkin_volume"] == 2
    assert body["alert_volume"] == 1


def test_expired_trial_does_not_count_as_active(client, db, make_admin, make_school, monkeypatch):
    make_admin(role=AdminRole.superadmin)
    school = make_school()
    now = datetime.now(UTC)
    db.add(Subscription(
        school_id=school.id, state=SubscriptionState.trial,
        trial_start_at=now - timedelta(weeks=7), trial_end_at=now - timedelta(weeks=1),
    ))
    db.commit()
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.json()["active_trials"] == 0


# ---- RBAC deny-cell (view_statistics) ------------------------------------------------------------

def test_stats_denied_for_role_without_view_statistics(client, db, make_admin, monkeypatch):
    # `support` DOES hold `view_statistics` in the FR-19-01 matrix — construct a role WITHOUT it by
    # monkeypatching the matrix, the same technique test_admin_school_ops.py uses for its own
    # deny-cell (support DOES have manage_accounts; there is no built-in AdminRole lacking every
    # permission to exercise this cell honestly otherwise).
    import src.application.authz.admin as admin_authz_mod

    make_admin(role=AdminRole.support)
    monkeypatch.setitem(admin_authz_mod._ROLE_PERMISSIONS, AdminRole.support, frozenset())
    token = _complete_signin(client, monkeypatch)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403
    row = db.query(AuditLog).filter(
        AuditLog.action == "admin.rbac.denied:view_statistics"
    ).one_or_none()
    assert row is not None  # the denial is audit-logged, not a silent 403


def test_stats_requires_admin_session(client, db, make_school, make_staff, monkeypatch):
    # a staff (non-admin) session must never reach the admin console
    school = make_school()
    staff = make_staff(school)
    token = client.post(
        "/api/v1/auth/staff/sign-in", json={"email": staff.email, "password": "Password123"}
    ).json()["access_token"]
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403
