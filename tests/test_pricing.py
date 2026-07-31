"""FR-17-06 — informational, read-only pricing presentation. GET /api/v1/pricing.

  Scenario 1  Pricing is per student per year, cheaper at higher volumes, by quote.
              -> test_leadership_gets_the_pricing_model
              -> test_district_gets_the_pricing_model
  Scenario 2  (NEG — no PCI surface) No in-platform card entry anywhere in the platform.
              -> test_no_card_or_payment_route_exists_anywhere_in_the_app
              -> test_response_never_contains_a_ratified_price_number
  Role scope  leadership/district only (ticket DoD) — 403 for teacher/support.
              -> test_teacher_is_forbidden
              -> test_support_is_forbidden
  Logging     fr_17_06_success / fr_17_06_forbidden.
              -> test_success_is_logged
              -> test_forbidden_is_logged

NOTE (scope note, not silently guessed): this is a stateless informational GET with no request
body/state to violate, so there is no natural trigger for `fr_17_06_rejected` — unlike FR-17-04's
downgrade/cancel (which reject an ineligible *state*), there is no ineligible state here to
reject. Only success/forbidden/error are exercised; flagged in the gate doc, not invented.
"""
from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.application.billing import pricing as pricing_svc
from src.constants.enums import SchoolStatus, SessionKind, StaffRole
from src.domain.identity.models import StaffAccount

ENDPOINT = "/api/v1/pricing"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _make(db, make_school, make_staff, *, code: str, role: StaffRole):
    school = make_school(code=code, status=SchoolStatus.active, name=f"Pricing {code}")
    staff = make_staff(school, email=f"staff-{code}@school.edu", role=role)
    return school, staff, _mint(db, staff)


# --- Scenario 1: the pricing model -----------------------------------------------------------


def test_leadership_gets_the_pricing_model(client, db, make_school, make_staff):
    _school, staff, token = _make(db, make_school, make_staff, code="PRC-1",
                                   role=StaffRole.leadership)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 200
    assert r.json() == {
        "model": "per_student_per_year",
        "by_quote": True,
        "tax": "none at launch",
    }
    assert staff.role == StaffRole.leadership


def test_district_gets_the_pricing_model(client, db, make_school, make_staff):
    _school, _staff, token = _make(db, make_school, make_staff, code="PRC-2",
                                    role=StaffRole.district)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["model"] == "per_student_per_year"
    assert r.json()["by_quote"] is True


# --- Scenario 2 (NEG): no in-platform card entry, no ratified number -------------------------


def test_no_card_or_payment_route_exists_anywhere_in_the_app(client):
    """NEG — the platform must offer no self-service card entry: no payment gateway, card
    vaulting, or payment webhook route exists anywhere in the app (external, quote-based
    billing)."""
    paths = client.get("/openapi.json").json()["paths"].keys()
    banned = ("card", "payment", "checkout", "gateway", "vault")
    offenders = [p for p in paths if any(word in p.lower() for word in banned)]
    assert offenders == []


def test_response_never_contains_a_ratified_price_number(client, db, make_school, make_staff):
    """Do-NOT: the recommended per-student price bands are pending client ratification (BRD
    Appendix A) — the response presents the model, not a number."""
    _school, _staff, token = _make(db, make_school, make_staff, code="PRC-3",
                                    role=StaffRole.leadership)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"model", "by_quote", "tax"}
    # bool is an int subclass in Python — exclude it explicitly so `by_quote: true` doesn't
    # false-positive as a numeric price.
    assert not any(
        isinstance(v, int | float) and not isinstance(v, bool) for v in body.values()
    )


# --- Role scope: leadership/district only -----------------------------------------------------


def test_teacher_is_forbidden(client, db, make_school, make_staff):
    _school, _staff, token = _make(db, make_school, make_staff, code="PRC-4",
                                    role=StaffRole.teacher)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403


def test_support_is_forbidden(client, db, make_school, make_staff):
    _school, _staff, token = _make(db, make_school, make_staff, code="PRC-5",
                                    role=StaffRole.support)
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403


# --- structured logging --------------------------------------------------------------------------
# NOTE: `caplog` cannot be used here (same test-harness quirk `test_roster.py` documents: alembic's
# `fileConfig` disables every "youhue.*" module logger not in alembic.ini's `[loggers]` list at
# collection time) — assert the exact call via monkeypatch instead.


def test_success_is_logged(client, db, make_school, make_staff, monkeypatch):
    _school, _staff, token = _make(db, make_school, make_staff, code="PRC-6",
                                    role=StaffRole.leadership)
    calls: list[str] = []
    monkeypatch.setattr(
        pricing_svc.logger, "info", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 200
    assert any("fr_17_06_success" in c for c in calls)


def test_forbidden_is_logged(client, db, make_school, make_staff, monkeypatch):
    _school, _staff, token = _make(db, make_school, make_staff, code="PRC-7",
                                    role=StaffRole.teacher)
    calls: list[str] = []
    monkeypatch.setattr(
        pricing_svc.logger, "warning", lambda msg, *a, **k: calls.append(msg % a if a else msg)
    )
    r = client.get(ENDPOINT, headers=_auth(token))
    assert r.status_code == 403
    assert any("fr_17_06_forbidden" in c for c in calls)
