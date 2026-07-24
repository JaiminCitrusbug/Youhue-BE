"""School leadership hub (FR-16-02): staff management (SC-057) + the leadership-exposed settings
surfaces (SC-060 concern words · SC-061 alert routing · SC-063 access window).

Every ticket Must-not has a test that fails if the guarantee is removed:

  MN-1  Deactivating a staff member updates status AND withdraws access IMMEDIATELY — the very
        next authenticated request on their existing session token is denied, with no separate
        revoke step. -> test_deactivate_withdraws_access_immediately_on_the_next_request
  MN-2  A changed leadership-exposed setting is saved and applies to the school (concern words /
        alert routing / access window). -> test_update_* below
  MN-3  School leadership may act on their OWN school only — 403 cross-tenant / non-leadership.
        -> test_*_forbidden_for_non_leadership, test_*_forbidden_cross_tenant
  MN-4  422 invalid setting value / invalid status value.
"""
import uuid

import pytest

from config.env_config import settings
from src.application.auth import sessions
from src.constants.enums import SchoolStatus, SessionKind, StaffRole, StaffStatus
from src.domain.identity.models import StaffAccount
from src.domain.org.models import CalendarConfig
from src.domain.risk.models import AlertRecipientConfig, ConcernWordList

SIGNIN = "/api/v1/auth/staff/sign-in"


def _staff_url(school_id: object) -> str:
    return f"/api/v1/schools/{school_id}/staff"


def _staff_detail_url(school_id: object, staff_id: object) -> str:
    return f"/api/v1/schools/{school_id}/staff/{staff_id}"


def _settings_url(school_id: object) -> str:
    return f"/api/v1/schools/{school_id}/settings"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    """Issue a full (non-MFA-pending) staff session directly, bypassing sign-in. Leadership forces
    email-OTP MFA at sign-in (settings.mfa_required_roles); that flow is INFRA-03's own concern
    (tests/test_authz.py) — this ticket only needs an authenticated actor of the right role."""
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _leader(db, make_school, make_staff, *, code: str = "LEAD-1"):
    school = make_school(code=code, status=SchoolStatus.active, name=f"Leadership {code}")
    leader = make_staff(school, email=f"head-{code}@school.edu", role=StaffRole.leadership)
    return school, leader, _mint(db, leader)


# =================================================================================================
# SC-057 — staff management
# =================================================================================================


def test_staff_list_returns_every_account_at_the_school(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff)
    teacher = make_staff(school, email="teacher@school.edu", role=StaffRole.teacher)

    r = client.get(_staff_url(school.id), headers=_auth(token))
    assert r.status_code == 200
    emails = {row["email"] for row in r.json()["staff"]}
    assert emails == {leader.email, teacher.email}


def test_deactivate_updates_status_to_deactivated(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="LEAD-2")
    teacher = make_staff(school, email="leaver@school.edu", role=StaffRole.teacher)

    r = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 200
    assert r.json()["staff"]["status"] == "deactivated"

    db.refresh(teacher)
    assert teacher.status == StaffStatus.deactivated


def test_deactivate_withdraws_access_immediately_on_the_next_request(
    client, db, make_school, make_staff
):
    """MN-1: the deactivated staff member's OWN existing session token stops resolving on the very
    next authenticated request — no separate revoke step, the status flip IS the revocation."""
    school, leader, leader_token = _leader(db, make_school, make_staff, code="LEAD-3")
    teacher = make_staff(school, email="soon-gone@school.edu", role=StaffRole.teacher)
    teacher_token = _mint(db, teacher)

    # Before deactivation the teacher's own session resolves fine on any StaffDep-gated route open
    # to every role (not the leadership-only staff list, which a teacher never had access to) —
    # `GET /notifications` (any staff, INFRA-05) is the probe; `/me` deliberately does NOT re-check
    # StaffStatus (it resolves the session only), so it cannot prove this guarantee.
    still_ok = client.get("/api/v1/notifications", headers=_auth(teacher_token))
    assert still_ok.status_code == 200

    deactivate = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "deactivated"}, headers=_auth(leader_token),
    )
    assert deactivate.status_code == 200

    denied = client.get("/api/v1/notifications", headers=_auth(teacher_token))
    assert denied.status_code == 403


def test_deactivate_is_idempotent_on_retry(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="LEAD-4")
    teacher = make_staff(school, email="retry@school.edu", role=StaffRole.teacher)

    first = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "deactivated"}, headers=_auth(token)
    )
    second = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "deactivated"}, headers=_auth(token)
    )
    assert first.status_code == 200
    assert second.status_code == 200


def test_deactivate_unsupported_status_value_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="LEAD-5")
    teacher = make_staff(school, email="active-again@school.edu", role=StaffRole.teacher)

    r = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "active"}, headers=_auth(token)
    )
    assert r.status_code == 422


def test_deactivate_unknown_staff_is_404(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="LEAD-6")
    r = client.patch(
        _staff_detail_url(school.id, uuid.uuid4()), json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 404


@pytest.mark.authz
@pytest.mark.parametrize("role", [StaffRole.teacher, StaffRole.support, StaffRole.district])
def test_deactivate_forbidden_for_non_leadership(client, db, make_school, make_staff, role):
    school = make_school(code="LEAD-7", status=SchoolStatus.active)
    actor = make_staff(school, email=f"actor-{role.value}@school.edu", role=role)
    token = _mint(db, actor)
    teacher = make_staff(school, email="target@school.edu", role=StaffRole.teacher)

    r = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 403


def test_deactivate_forbidden_cross_tenant(client, db, make_school, make_staff):
    school_a, leader_a, token_a = _leader(db, make_school, make_staff, code="LEAD-8A")
    school_b, leader_b, _ = _leader(db, make_school, make_staff, code="LEAD-8B")
    victim = make_staff(school_b, email="victim@other.edu", role=StaffRole.teacher)

    r = client.patch(
        _staff_detail_url(school_b.id, victim.id), json={"status": "deactivated"}, headers=_auth(token_a)
    )
    assert r.status_code == 403
    db.refresh(victim)
    assert victim.status == StaffStatus.active


def test_staff_list_forbidden_cross_tenant(client, db, make_school, make_staff):
    school_a, leader_a, token_a = _leader(db, make_school, make_staff, code="LEAD-9A")
    school_b, leader_b, _ = _leader(db, make_school, make_staff, code="LEAD-9B")

    r = client.get(_staff_url(school_b.id), headers=_auth(token_a))
    assert r.status_code == 403


def test_deactivate_unexpected_failure_rolls_back(client, db, make_school, make_staff, monkeypatch):
    import src.application.leadership.services as leadership_svc

    school, leader, token = _leader(db, make_school, make_staff, code="LEAD-10")
    teacher = make_staff(school, email="boom@school.edu", role=StaffRole.teacher)

    def _boom(*_a: object, **_k: object) -> StaffAccount:
        raise RuntimeError("db went away")

    monkeypatch.setattr(leadership_svc, "deactivate_staff", _boom)

    r = client.patch(
        _staff_detail_url(school.id, teacher.id), json={"status": "deactivated"}, headers=_auth(token)
    )
    assert r.status_code == 500
    db.refresh(teacher)
    assert teacher.status == StaffStatus.active


# =================================================================================================
# SC-060 / SC-061 / SC-063 — settings hub
# =================================================================================================


def test_get_settings_default_snapshot_is_empty(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-1")
    r = client.get(_settings_url(school.id), headers=_auth(token))
    assert r.status_code == 200
    body = r.json()["settings"]
    assert body["concern_words"] == {"platform_defaults": [], "school_additions": []}
    assert body["alert_routing"] == []
    assert body["access_window"] is None


def test_update_concern_words_persists_the_addition_and_shows_it_separately_from_defaults(
    client, db, make_school, make_staff
):
    db.add(ConcernWordList(school_id=None, words=["hurt", "unsafe"], is_default=True))
    db.commit()
    school, leader, token = _leader(db, make_school, make_staff, code="SET-2")

    r = client.patch(
        _settings_url(school.id), json={"concern_words": {"words": ["kms"]}}, headers=_auth(token)
    )
    assert r.status_code == 200
    cw = r.json()["settings"]["concern_words"]
    assert cw["platform_defaults"] == ["hurt", "unsafe"]
    assert cw["school_additions"] == ["kms"]

    row = db.query(ConcernWordList).filter(
        ConcernWordList.school_id == school.id, ConcernWordList.is_default.is_(False)
    ).one()
    # The persisted override is the FULL resolved set (defaults union additions) — resolve_words()
    # treats an override as a replacement, not an addend, so defaults must ride along on every save.
    assert set(row.words) == {"hurt", "unsafe", "kms"}


def test_update_concern_words_with_no_platform_default_and_empty_additions_is_422(
    client, db, make_school, make_staff
):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-3")
    r = client.patch(
        _settings_url(school.id), json={"concern_words": {"words": []}}, headers=_auth(token)
    )
    assert r.status_code == 422


def test_update_alert_routing_persists_ordered_recipients(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-4")
    teacher = make_staff(school, email="t1@school.edu", role=StaffRole.teacher)
    pastoral = make_staff(school, email="t2@school.edu", role=StaffRole.support)

    body = {
        "alert_routing": {
            "routes": [
                {"alert_type": "immediate", "recipient_staff_ids": [str(teacher.id), str(pastoral.id)]},
            ]
        }
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 200
    routing = r.json()["settings"]["alert_routing"]
    assert routing == [
        {"alert_type": "immediate", "recipient_staff_ids": [str(teacher.id), str(pastoral.id)]}
    ]

    row = db.query(AlertRecipientConfig).filter(AlertRecipientConfig.school_id == school.id).one()
    assert row.recipient_staff_ids == [teacher.id, pastoral.id]


def test_update_alert_routing_unknown_alert_type_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-5")
    body = {"alert_routing": {"routes": [{"alert_type": "urgent", "recipient_staff_ids": [str(leader.id)]}]}}
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


def test_update_alert_routing_cross_tenant_recipient_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-6A")
    other_school, other_leader, _ = _leader(db, make_school, make_staff, code="SET-6B")

    body = {
        "alert_routing": {
            "routes": [{"alert_type": "immediate", "recipient_staff_ids": [str(other_leader.id)]}]
        }
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


def test_update_access_window_persists(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-7")
    body = {
        "access_window": {"window_start": "08:30", "window_end": "09:30", "timezone": "Europe/London"}
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 200
    window = r.json()["settings"]["access_window"]
    assert window == {
        "window_start": "08:30:00", "window_end": "09:30:00", "timezone": "Europe/London",
        "term_start": None, "term_end": None,
    }

    row = db.query(CalendarConfig).filter(CalendarConfig.school_id == school.id).one()
    assert row.timezone == "Europe/London"


def test_update_access_window_start_after_end_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-8")
    body = {
        "access_window": {"window_start": "10:00", "window_end": "09:00", "timezone": "UTC"}
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


def test_update_access_window_with_term_dates_persists_both(client, db, make_school, make_staff):
    """FR-07-04: the minimal term-dates surface added to the SAME CalendarConfig row/setting."""
    school, leader, token = _leader(db, make_school, make_staff, code="SET-7B")
    body = {
        "access_window": {
            "window_start": "08:30", "window_end": "09:30", "timezone": "Europe/London",
            "term_start": "2026-09-01", "term_end": "2026-12-18",
        }
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 200
    window = r.json()["settings"]["access_window"]
    assert window["term_start"] == "2026-09-01"
    assert window["term_end"] == "2026-12-18"

    row = db.query(CalendarConfig).filter(CalendarConfig.school_id == school.id).one()
    assert str(row.term_start) == "2026-09-01"
    assert str(row.term_end) == "2026-12-18"


def test_update_access_window_omitting_term_dates_leaves_them_unchanged(
    client, db, make_school, make_staff
):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-7C")
    first = {
        "access_window": {
            "window_start": "08:30", "window_end": "09:30", "timezone": "UTC",
            "term_start": "2026-09-01", "term_end": "2026-12-18",
        }
    }
    client.patch(_settings_url(school.id), json=first, headers=_auth(token))

    # A later save that only touches window/timezone must not silently erase the saved term dates.
    second = {"access_window": {"window_start": "08:00", "window_end": "09:00", "timezone": "UTC"}}
    r = client.patch(_settings_url(school.id), json=second, headers=_auth(token))
    assert r.status_code == 200
    window = r.json()["settings"]["access_window"]
    assert window["term_start"] == "2026-09-01"
    assert window["term_end"] == "2026-12-18"


def test_update_access_window_term_start_after_end_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-7D")
    body = {
        "access_window": {
            "window_start": "08:30", "window_end": "09:30", "timezone": "UTC",
            "term_start": "2026-12-18", "term_end": "2026-09-01",
        }
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


def test_update_access_window_one_sided_term_dates_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-7E")
    body = {
        "access_window": {
            "window_start": "08:30", "window_end": "09:30", "timezone": "UTC",
            "term_start": "2026-09-01",
        }
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


def test_update_access_window_unknown_timezone_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-9")
    body = {
        "access_window": {"window_start": "08:00", "window_end": "09:00", "timezone": "Nowhere/Nothing"}
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


def test_update_settings_with_zero_settings_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-10")
    r = client.patch(_settings_url(school.id), json={}, headers=_auth(token))
    assert r.status_code == 422


def test_update_settings_with_two_settings_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="SET-11")
    body = {
        "concern_words": {"words": ["x"]},
        "access_window": {"window_start": "08:00", "window_end": "09:00", "timezone": "UTC"},
    }
    r = client.patch(_settings_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 422


@pytest.mark.authz
@pytest.mark.parametrize("role", [StaffRole.teacher, StaffRole.support, StaffRole.district])
def test_settings_forbidden_for_non_leadership(client, db, make_school, make_staff, role):
    school = make_school(code="SET-12", status=SchoolStatus.active)
    actor = make_staff(school, email=f"actor-{role.value}@school.edu", role=role)
    token = _mint(db, actor)

    r = client.get(_settings_url(school.id), headers=_auth(token))
    assert r.status_code == 403

    w = client.patch(
        _settings_url(school.id), json={"concern_words": {"words": ["x"]}}, headers=_auth(token)
    )
    assert w.status_code == 403


def test_settings_forbidden_cross_tenant(client, db, make_school, make_staff):
    school_a, leader_a, token_a = _leader(db, make_school, make_staff, code="SET-13A")
    school_b, leader_b, _ = _leader(db, make_school, make_staff, code="SET-13B")

    r = client.get(_settings_url(school_b.id), headers=_auth(token_a))
    assert r.status_code == 403


def test_update_settings_unexpected_failure_rolls_back(client, db, make_school, make_staff, monkeypatch):
    import src.application.leadership.services as leadership_svc

    school, leader, token = _leader(db, make_school, make_staff, code="SET-14")

    def _boom(*_a: object, **_k: object):
        raise RuntimeError("db went away")

    monkeypatch.setattr(leadership_svc, "update_settings", _boom)

    r = client.patch(
        _settings_url(school.id), json={"concern_words": {"words": ["x"]}}, headers=_auth(token)
    )
    assert r.status_code == 500
    assert db.query(ConcernWordList).filter(ConcernWordList.school_id == school.id).count() == 0


def test_unauthenticated_calls_are_refused(client, make_school):
    school = make_school(code="SET-15", status=SchoolStatus.active)
    assert client.get(_staff_url(school.id)).status_code in (401, 403)
    assert client.get(_settings_url(school.id)).status_code in (401, 403)


def test_openapi_documents_the_leadership_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    staff_patch = paths["/api/v1/schools/{school_id}/staff/{staff_id}"]["patch"]["responses"]
    assert {"200", "403", "404", "422", "500"} <= set(staff_patch)
    settings_patch = paths["/api/v1/schools/{school_id}/settings"]["patch"]["responses"]
    assert {"200", "403", "422", "500"} <= set(settings_patch)
