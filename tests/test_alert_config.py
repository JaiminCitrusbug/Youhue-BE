"""FR-12-05 (alert recipient config) — PUT /api/v1/schools/{id}/alert-config.

FR-16-02 already built the AlertRecipientConfig table + a settings-PATCH write path
(tests/test_leadership.py) but explicitly left two things to this ticket: a dedicated endpoint
contract, and the DB-level unique constraint its own docstring flagged as a KNOWN GAP. These tests
cover the dedicated endpoint + the constraint's concurrency guarantee; the underlying table's basic
persistence behavior is already covered by FR-16-02's own tests.

  MN-1  Leadership-only, school-scoped — 403 otherwise.
  MN-2  422 unsupported alert type / cross-tenant recipient / empty recipient list.
  MN-3  Two concurrent writes for the same (school_id, alert_type) never produce a duplicate row —
        the DB-backed unique constraint + real upsert this ticket adds.
"""
import threading
import uuid

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.constants.enums import SchoolStatus, SessionKind, StaffRole
from src.domain.identity.models import StaffAccount
from src.domain.risk.models import AlertRecipientConfig

SIGNIN = "/api/v1/auth/staff/sign-in"


def _url(school_id: object) -> str:
    return f"/api/v1/schools/{school_id}/alert-config"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _leader(db, make_school, make_staff, *, code: str = "AC-1"):
    school = make_school(code=code, status=SchoolStatus.active, name=f"AlertConfig {code}")
    leader = make_staff(school, email=f"head-{code}@school.edu", role=StaffRole.leadership)
    return school, leader, _mint(db, leader)


def test_set_alert_config_persists_ordered_recipients(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="AC-2")
    teacher = make_staff(school, email="t1@school.edu", role=StaffRole.teacher)
    pastoral = make_staff(school, email="t2@school.edu", role=StaffRole.support)

    body = {"alert_type": "immediate", "recipient_staff_ids": [str(teacher.id), str(pastoral.id)]}
    r = client.put(_url(school.id), json=body, headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["config"] == {
        "alert_type": "immediate",
        "recipient_staff_ids": [str(teacher.id), str(pastoral.id)],
    }

    row = db.scalar(
        select(AlertRecipientConfig).where(
            AlertRecipientConfig.school_id == school.id,
            AlertRecipientConfig.alert_type == "immediate",
        )
    )
    assert row.recipient_staff_ids == [teacher.id, pastoral.id]


def test_set_alert_config_replaces_existing_row_not_duplicates(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="AC-3")
    a = make_staff(school, email="a@school.edu", role=StaffRole.teacher)
    b = make_staff(school, email="b@school.edu", role=StaffRole.teacher)

    r1 = client.put(
        _url(school.id),
        json={"alert_type": "triage", "recipient_staff_ids": [str(a.id)]},
        headers=_auth(token),
    )
    assert r1.status_code == 200
    r2 = client.put(
        _url(school.id),
        json={"alert_type": "triage", "recipient_staff_ids": [str(b.id)]},
        headers=_auth(token),
    )
    assert r2.status_code == 200
    assert r2.json()["config"]["recipient_staff_ids"] == [str(b.id)]

    rows = db.scalars(
        select(AlertRecipientConfig).where(
            AlertRecipientConfig.school_id == school.id,
            AlertRecipientConfig.alert_type == "triage",
        )
    ).all()
    assert len(rows) == 1  # replaced in place, not a second row


def test_set_alert_config_forbidden_for_non_leadership(client, db, make_school, make_staff):
    school = make_school(code="AC-4", status=SchoolStatus.active, name="AlertConfig AC-4")
    teacher = make_staff(school, email="teach@school.edu", role=StaffRole.teacher)
    token = _mint(db, teacher)

    r = client.put(
        _url(school.id),
        json={"alert_type": "immediate", "recipient_staff_ids": [str(teacher.id)]},
        headers=_auth(token),
    )
    assert r.status_code == 403


def test_set_alert_config_forbidden_cross_tenant(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="AC-5A")
    other_school = make_school(code="AC-5B", status=SchoolStatus.active, name="AlertConfig AC-5B")

    r = client.put(
        _url(other_school.id),
        json={"alert_type": "immediate", "recipient_staff_ids": [str(leader.id)]},
        headers=_auth(token),
    )
    assert r.status_code == 403


def test_set_alert_config_unknown_alert_type_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="AC-6")
    r = client.put(
        _url(school.id),
        json={"alert_type": "urgent", "recipient_staff_ids": [str(leader.id)]},
        headers=_auth(token),
    )
    assert r.status_code == 422


def test_set_alert_config_cross_tenant_recipient_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="AC-7A")
    _, other_leader, _ = _leader(db, make_school, make_staff, code="AC-7B")

    r = client.put(
        _url(school.id),
        json={"alert_type": "immediate", "recipient_staff_ids": [str(other_leader.id)]},
        headers=_auth(token),
    )
    assert r.status_code == 422


def test_set_alert_config_empty_recipients_is_422(client, db, make_school, make_staff):
    school, leader, token = _leader(db, make_school, make_staff, code="AC-8")
    r = client.put(
        _url(school.id),
        json={"alert_type": "immediate", "recipient_staff_ids": []},
        headers=_auth(token),
    )
    assert r.status_code == 422


def test_concurrent_writes_same_alert_type_never_duplicate_row(db, make_school, make_staff):
    """Real concurrency, real separate DB sessions — the FR-12-05 fix (a DB unique constraint +
    ON CONFLICT DO UPDATE) must hold under genuine concurrent writers, not just sequential retries;
    matches this codebase's established methodology (test_checkin_race.py et al.) for this bug
    class. Drives `risk_svc.set_alert_config` directly (not through the HTTP client, which shares
    one session) so each thread genuinely owns its own connection/transaction."""
    from src.application.risk import services as risk_svc

    school = make_school(code="AC-9", status=SchoolStatus.active, name="AlertConfig AC-9")
    leader = make_staff(school, email="head-ac9@school.edu", role=StaffRole.leadership)
    s1 = make_staff(school, email="race1@school.edu", role=StaffRole.teacher)
    s2 = make_staff(school, email="race2@school.edu", role=StaffRole.teacher)
    db.commit()

    engine = create_engine(env_settings.database_url_test, pool_pre_ping=True)
    Session = sessionmaker(bind=engine)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def _worker(recipient_id: uuid.UUID) -> None:
        sess = Session()
        try:
            leader_row = sess.get(StaffAccount, leader.id)
            barrier.wait(timeout=5)
            risk_svc.set_alert_config(sess, leader_row, school.id, "immediate", [recipient_id])
            sess.commit()
        except BaseException as exc:  # noqa: BLE001 - captured and re-raised on the main thread
            sess.rollback()
            errors.append(exc)
        finally:
            sess.close()

    t1 = threading.Thread(target=_worker, args=(s1.id,))
    t2 = threading.Thread(target=_worker, args=(s2.id,))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    assert not errors, f"concurrent writers raised: {errors}"
    rows = db.scalars(
        select(AlertRecipientConfig).where(
            AlertRecipientConfig.school_id == school.id,
            AlertRecipientConfig.alert_type == "immediate",
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].recipient_staff_ids in ([s1.id], [s2.id])  # one writer's value won, cleanly
