"""FR-17-01 (Free vs Premium entitlement engine) — GET /api/v1/schools/{id}/entitlements.

  MN-1  Free tier: exactly the DoD-listed core feature set, no Premium features.
  MN-2  Premium tier: Free set + the DoD-listed advanced set on top.
  MN-3  School-scoped — 403 for a caller at a different school.
  MN-4  Tier is read from the one-owner `Subscription.tier` column, not re-decided per caller.
"""
from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.constants.enums import SchoolStatus, SessionKind, StaffRole, SubscriptionTier
from src.domain.billing.models import Subscription
from src.domain.identity.models import StaffAccount

SIGNIN = "/api/v1/auth/staff/sign-in"


def _url(school_id: object) -> str:
    return f"/api/v1/schools/{school_id}/entitlements"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _staff(db, make_school, make_staff, *, code: str, role: StaffRole = StaffRole.teacher):
    school = make_school(code=code, status=SchoolStatus.active, name=f"Entitlements {code}")
    staff = make_staff(school, email=f"staff-{code}@school.edu", role=role)
    return school, staff, _mint(db, staff)


def test_free_tier_returns_exactly_the_core_feature_set(client, db, make_school, make_staff):
    school, staff, token = _staff(db, make_school, make_staff, code="ENT-1")
    db.add(Subscription(school_id=school.id, tier=SubscriptionTier.free))
    db.commit()

    r = client.get(_url(school.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "free"
    assert set(body["features"]) == {
        "daily_checkins",
        "basic_mood_trends",
        "unlimited_students_and_classes",
        "colleague_invites",
        "young_child_simple_mode",
        "basic_activity_set",
    }
    # never leaks a Premium-only feature onto a Free response
    assert "ai_analysis" not in body["features"]
    assert "custom_checkins" not in body["features"]


def test_premium_tier_adds_the_advanced_set_on_top_of_free(client, db, make_school, make_staff):
    school, staff, token = _staff(db, make_school, make_staff, code="ENT-2")
    db.add(Subscription(school_id=school.id, tier=SubscriptionTier.premium))
    db.commit()

    r = client.get(_url(school.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "premium"
    features = set(body["features"])
    # Free set still present — Premium ADDS, never replaces
    assert {"daily_checkins", "colleague_invites", "young_child_simple_mode"} <= features
    # the DoD-listed advanced set is present
    assert {
        "custom_checkins",
        "alerts_and_intervention_tracking",
        "ai_analysis",
        "richer_dashboards",
        "reports",
        "school_district_views",
        "roster_sync",
        "calendar",
        "access_windows",
        "onboarding",
    } <= features


def test_no_subscription_row_yet_defaults_to_free(client, db, make_school, make_staff):
    """A school with no explicit trial/approval action (no Subscription row at all) still
    resolves — the column default is `free`, and the lazy first-touch creator (already proven by
    FR-19-02) fires here for the first time on a plain read, not just an admin-console write."""
    school, staff, token = _staff(db, make_school, make_staff, code="ENT-3")

    r = client.get(_url(school.id), headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["tier"] == "free"


def test_cross_school_staff_gets_403(client, db, make_school, make_staff):
    school_a, _, _ = _staff(db, make_school, make_staff, code="ENT-4A")
    _, staff_b, token_b = _staff(db, make_school, make_staff, code="ENT-4B")

    r = client.get(_url(school_a.id), headers=_auth(token_b))
    assert r.status_code == 403


def test_any_staff_role_can_read_own_school_entitlements(client, db, make_school, make_staff):
    """Not leadership-gated — every role reads the same tier state (ticket: "staff use the
    platform" for Scenario 2, not "leadership use the platform")."""
    school, staff, token = _staff(db, make_school, make_staff, code="ENT-5", role=StaffRole.support)
    db.add(Subscription(school_id=school.id, tier=SubscriptionTier.free))
    db.commit()

    r = client.get(_url(school.id), headers=_auth(token))
    assert r.status_code == 200
