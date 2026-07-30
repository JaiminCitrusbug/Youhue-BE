"""FR-20-02 — school exit: export-then-hard-delete (SC-065, ticket DoD). Reuses FR-20-01's export
machinery (`kind=export_and_delete`) and adds the ordered, irreversible second step.

Covers @FR-20-02:
  Scenario 1 (positive): exit processed -> the system offers a full export before any deletion.
  Scenario 2 (positive): once the export is `ready` -> the deletion step hard-deletes the school's
    data — proven here by querying the DB directly for ABSENCE across every school-scoped table,
    not just asserting a 200 came back.
  Scenario 3 (NEG — ordering guard): a delete attempted before the export is ready is refused (409).
Plus: leadership/own-school authz (role checked before school scope, GATE-12 shape), cross-tenant
isolation (deleting school A never touches school B), the immutable audit trail surviving the
school it describes, the deliberately-untouched platform-shared rows (seed Activity / default
ConcernWordList / LoginAttempt), and a genuine concurrency race on the "start the exit" step.
"""
import threading
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config.env_config import settings as env_settings
from src.application.auth import sessions
from src.application.compliance import deletion_services as deletion_svc
from src.constants.enums import (
    ActivityAgeBand,
    ActivityScope,
    ActivityType,
    AlertChannel,
    DataExportKind,
    DataExportStatus,
    DeliveryStatus,
    FlagEventType,
    FlagType,
    InvitationStatus,
    SessionKind,
    StaffClassScope,
    StaffRole,
)
from src.domain.auth.models import AuthSession, LoginAttempt, MfaOtp, PasswordResetToken
from src.domain.billing.models import Notification, Subscription
from src.domain.checkin.models import Activity, ActivityEngagement, CheckIn, CheckInSettings
from src.domain.compliance.models import AuditLog, DataExport, ParentalConsent
from src.domain.identity.models import School, StaffAccount, Student
from src.domain.org.models import (
    CalendarConfig,
    ClassGroup,
    ClassMembership,
    Invitation,
    StaffClassAccess,
)
from src.domain.risk.models import (
    AlertDelivery,
    AlertRecipientConfig,
    ConcernWordList,
    Flag,
    FlagEvent,
    SupportiveNote,
)

STAFF_SIGNIN = "/api/v1/auth/staff/sign-in"


@pytest.fixture(autouse=True)
def _background_task_uses_test_db(monkeypatch):
    """Same reasoning as `test_data_export.py`: the export-build background task opens its own
    session via `config.db_connection.SessionLocal`, which must be the TEST engine here too."""
    from tests.conftest import _TestSession

    monkeypatch.setattr("config.db_connection.SessionLocal", _TestSession)


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint(db, staff: StaffAccount) -> str:
    sess = sessions.create_session(
        db, staff.id, SessionKind.staff, env_settings.staff_session_ttl_minutes,
        school_id=staff.school_id,
    )
    db.commit()
    return sessions.issue_token(sess)


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=1)


def _seed_full_footprint(
    db, school: School, leader: StaffAccount, student: Student,
    include_platform_shared: bool = True,
) -> dict:
    """One row in every table reachable from `school`/`leader`/`student`, PLUS one row each in the
    tables that must NEVER be touched by this school's delete (a second school's data, the shared
    platform seed Activity, the shared platform default ConcernWordList, and a LoginAttempt keyed
    only by email). Returns the ids the test needs to assert on afterward."""
    # per-school-unique — this helper is called once per school in the same test, and a shared
    # literal here would make both schools' LoginAttempt rows collide on the same identifier.
    teacher_email = f"teacher-{school.sign_in_code}@fullfootprint.edu"
    teacher = StaffAccount(
        school_id=school.id, email=teacher_email, role=StaffRole.teacher,
        password_hash="x",
    )
    db.add(teacher)
    db.flush()

    klass = ClassGroup(school_id=school.id, name="3A")
    db.add(klass)
    db.flush()
    db.add(ClassMembership(class_id=klass.id, student_id=student.id))
    db.add(StaffClassAccess(staff_id=teacher.id, class_id=klass.id, scope=StaffClassScope.owner))

    checkin = CheckIn(
        student_id=student.id, school_id=school.id, mood_value=3, reflection_text="ok",
        local_date=date.today(),
    )
    db.add(checkin)
    db.flush()

    flag = Flag(
        student_id=student.id, school_id=school.id, checkin_id=checkin.id,
        type=FlagType.concern_word, risk_score=Decimal("0.75"),
    )
    db.add(flag)
    db.flush()

    db.add(FlagEvent(flag_id=flag.id, event_type=FlagEventType.viewed, actor_id=leader.id))
    db.add(
        SupportiveNote(
            student_id=student.id, sender_id=leader.id, flag_id=flag.id, body="hang in there"
        )
    )
    db.add(ParentalConsent(student_id=student.id))

    school_activity = Activity(
        scope=ActivityScope.school, school_id=school.id, title="School-only breathing",
        type=ActivityType.breathing, age_band=ActivityAgeBand.all,
    )
    db.add(school_activity)
    seed_activity = None
    if include_platform_shared:
        # scope=seed has no uniqueness constraint — safe to add regardless — but kept behind the
        # same flag as the default ConcernWordList below for symmetry/clarity.
        seed_activity = Activity(
            scope=ActivityScope.seed, school_id=None, title="Platform seed breathing",
            type=ActivityType.breathing, age_band=ActivityAgeBand.all,
        )
        db.add(seed_activity)
    db.flush()
    db.add(
        ActivityEngagement(
            student_id=student.id, activity_id=school_activity.id, checkin_id=checkin.id
        )
    )

    notification = Notification(recipient_id=leader.id, type="flag_alert")
    db.add(notification)
    db.flush()
    db.add(
        AlertDelivery(
            notification_id=notification.id, flag_id=flag.id, recipient_id=leader.id,
            channel=AlertChannel.in_app, status=DeliveryStatus.queued,
        )
    )

    db.add(
        Invitation(
            school_id=school.id, class_id=klass.id, inviter_id=leader.id,
            email="invitee@fullfootprint.edu", token=f"tok-{uuid.uuid4()}",
            status=InvitationStatus.invited, expires_at=_future(),
        )
    )
    db.add(CalendarConfig(school_id=school.id, window_start=time(7, 0), window_end=time(9, 0)))
    db.add(CheckInSettings(school_id=school.id, require_reflection=True))
    db.add(Subscription(school_id=school.id))
    db.add(
        AlertRecipientConfig(
            school_id=school.id, alert_type="immediate", recipient_staff_ids=[leader.id]
        )
    )
    db.add(ConcernWordList(school_id=school.id, words=["sad"], is_default=False))
    default_words = None
    if include_platform_shared:
        # at most ONE is_default=True row platform-wide (uq_concern_word_lists_default) — only
        # ever created once per test, not once per school seeded.
        default_words = ConcernWordList(school_id=None, words=["worried"], is_default=True)
        db.add(default_words)

    db.add(
        AuthSession(
            subject_id=leader.id, kind=SessionKind.staff, school_id=school.id,
            expires_at=_future(),
        )
    )
    db.add(MfaOtp(subject_id=leader.id, code_hash="h", expires_at=_future()))
    db.add(PasswordResetToken(staff_id=leader.id, token_hash=f"rt-{uuid.uuid4()}", expires_at=_future()))
    db.add(LoginAttempt(identifier=teacher_email, succeeded=True))

    export = DataExport(
        school_id=school.id, requested_by=leader.id, kind=DataExportKind.export,
        status=DataExportStatus.ready, storage_key="exports/whatever.json",
    )
    db.add(export)

    db.flush()
    return {
        "teacher_id": teacher.id, "class_id": klass.id, "checkin_id": checkin.id,
        "flag_id": flag.id, "school_activity_id": school_activity.id,
        "seed_activity_id": seed_activity.id if seed_activity else None,
        "notification_id": notification.id,
        "plain_export_id": export.id,
        "default_word_list_id": default_words.id if default_words else None,
    }


def _row_count(db, model, **filters) -> int:
    stmt = select(sa.func.count()).select_from(model)
    for k, v in filters.items():
        stmt = stmt.where(getattr(model, k) == v)
    return db.scalar(stmt) or 0


# ---- Scenario 1: export offered before any deletion ----------------------------------------------

def test_export_offered_before_any_deletion(client, db, make_school, make_staff, make_student):
    school = make_school()
    leader = make_staff(school, email="lead@oakwood.edu", role=StaffRole.leadership)
    make_student(school)
    token = _mint(db, leader)

    r = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"] is False
    assert body["status"] == "pending"

    row = db.scalar(select(DataExport).where(DataExport.id == uuid.UUID(body["export_id"])))
    assert row is not None
    assert row.kind == DataExportKind.export_and_delete
    # nothing was touched — the exit is offered, never runs the delete on the very first call
    assert db.get(School, school.id) is not None
    assert db.get(StaffAccount, leader.id) is not None


def test_offer_step_defaults_to_export_and_delete_kind(db, make_school, make_staff):
    school = make_school()
    leader = make_staff(school, email="lead2@oakwood.edu", role=StaffRole.leadership)
    export, deleted, needs_build = deletion_svc.request_export_and_delete(db, leader, school.id)
    db.commit()
    assert deleted is False
    assert needs_build is True
    assert export.kind == DataExportKind.export_and_delete
    assert export.status == DataExportStatus.pending


# ---- Scenario 3 (NEG — ordering guard): refused before the export is ready -----------------------

def test_deletion_refused_before_export_is_ready(db, make_school, make_staff):
    school = make_school(code="ORD-1")
    leader = make_staff(school, email="lead3@oakwood.edu", role=StaffRole.leadership)
    # start the exit at the service layer WITHOUT running the background build (mirrors FR-20-01's
    # test_pending_export_has_no_download_url_yet) — the export genuinely stays `pending`.
    deletion_svc.request_export_and_delete(db, leader, school.id)
    db.commit()

    with pytest.raises(HTTPException) as exc:
        deletion_svc.request_export_and_delete(db, leader, school.id)
    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "export_not_retrieved"
    # still nothing deleted
    assert db.get(School, school.id) is not None


def test_deletion_refused_via_http_before_export_ready(client, db, make_school, make_staff):
    school = make_school(code="ORD-2")
    leader = make_staff(school, email="lead4@oakwood.edu", role=StaffRole.leadership)
    token = _mint(db, leader)
    deletion_svc.request_export_and_delete(db, leader, school.id)
    db.commit()

    r = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "export_not_retrieved"


# ---- Scenario 2: once the export is ready, the deletion step hard-deletes everything -------------

def test_hard_delete_removes_every_school_scoped_row(client, db, make_school, make_staff, make_student):
    """The real, execution-proven assertion for a destructive-delete ticket: query the DB AFTER and
    assert absence across every table reachable from the school — not just a 200/deleted=True."""
    school = make_school(code="HD-1")
    leader = make_staff(school, email="lead5@oakwood.edu", role=StaffRole.leadership)
    student = make_student(school)
    ids = _seed_full_footprint(db, school, leader, student)
    db.commit()
    # captured as plain values BEFORE the delete — after it, these ORM instances are expired AND
    # gone, so reading .id on them (not just db.get()) raises ObjectDeletedError, same root cause
    # as the db.get() issue above but on plain attribute access.
    school_id, leader_id, student_id = school.id, leader.id, student.id

    # a SECOND, untouched school + its own leader/student, to prove isolation (FR-20-07) in the
    # SAME assertion pass as the deletion proof below.
    other_school = make_school(code="HD-2")
    other_leader = make_staff(other_school, email="other@oakwood.edu", role=StaffRole.leadership)
    other_student = make_student(other_school)
    other_ids = _seed_full_footprint(
        db, other_school, other_leader, other_student, include_platform_shared=False
    )
    db.commit()

    token = _mint(db, leader)
    r1 = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r1.status_code == 200
    assert r1.json()["deleted"] is False
    # TestClient runs BackgroundTasks synchronously before returning control here, so the export
    # artifact is already `ready` by now (same behaviour test_data_export.py documents/relies on).

    r2 = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["deleted"] is True
    assert body2["status"] == "completed"

    db.expire_all()

    # ---- the school and everything school-scoped is GONE -----------------------------------
    # Use the plain UUIDs captured before the delete, not the ORM objects — after the delete these
    # instances are expired AND gone, so any attribute access (db.get(), or reading school.id
    # itself) raises ObjectDeletedError rather than cleanly indicating absence.
    assert _row_count(db, School, id=school_id) == 0
    assert _row_count(db, StaffAccount, id=leader_id) == 0
    assert _row_count(db, StaffAccount, id=ids["teacher_id"]) == 0
    assert _row_count(db, Student, id=student_id) == 0
    assert _row_count(db, ClassGroup, id=ids["class_id"]) == 0
    assert _row_count(db, ClassMembership, class_id=ids["class_id"]) == 0
    assert _row_count(db, StaffClassAccess, class_id=ids["class_id"]) == 0
    assert _row_count(db, CheckIn, id=ids["checkin_id"]) == 0
    assert _row_count(db, Flag, id=ids["flag_id"]) == 0
    assert _row_count(db, FlagEvent, flag_id=ids["flag_id"]) == 0
    assert _row_count(db, SupportiveNote, student_id=student_id) == 0
    assert _row_count(db, ParentalConsent, student_id=student_id) == 0
    assert _row_count(db, Activity, id=ids["school_activity_id"]) == 0
    assert _row_count(db, ActivityEngagement, student_id=student_id) == 0
    assert _row_count(db, Notification, id=ids["notification_id"]) == 0
    assert _row_count(db, AlertDelivery, recipient_id=leader_id) == 0
    assert _row_count(db, Invitation, school_id=school_id) == 0
    assert _row_count(db, CalendarConfig, school_id=school_id) == 0
    assert _row_count(db, CheckInSettings, school_id=school_id) == 0
    assert _row_count(db, Subscription, school_id=school_id) == 0
    assert _row_count(db, AlertRecipientConfig, school_id=school_id) == 0
    assert _row_count(db, ConcernWordList, school_id=school_id) == 0
    assert _row_count(db, AuthSession, subject_id=leader_id) == 0
    assert _row_count(db, MfaOtp, subject_id=leader_id) == 0
    assert _row_count(db, PasswordResetToken, staff_id=leader_id) == 0
    assert _row_count(db, DataExport, school_id=school_id) == 0
    assert _row_count(db, DataExport, id=ids["plain_export_id"]) == 0

    # ---- deliberately-untouched shared/global rows ------------------------------------------
    assert _row_count(db, Activity, id=ids["seed_activity_id"]) == 1  # platform seed content
    assert _row_count(db, ConcernWordList, id=ids["default_word_list_id"]) == 1  # platform default
    assert _row_count(db, LoginAttempt, identifier="teacher-HD-1@fullfootprint.edu") == 1  # never scoped

    # ---- the immutable audit trail survives the school it describes (BR-12) ------------------
    audit = db.scalar(
        select(AuditLog).where(
            AuditLog.action == "fr_20_02.export_and_delete",
            AuditLog.target == f"school:{school_id}",
        )
    )
    assert audit is not None
    assert audit.school_id is None  # FK detached, row preserved
    assert audit.actor_id == leader_id

    # ---- isolation: the OTHER school's full footprint is completely untouched ----------------
    assert db.get(School, other_school.id) is not None
    assert db.get(StaffAccount, other_leader.id) is not None
    assert db.get(StaffAccount, other_ids["teacher_id"]) is not None
    assert db.get(Student, other_student.id) is not None
    assert _row_count(db, CheckIn, id=other_ids["checkin_id"]) == 1
    assert _row_count(db, Flag, id=other_ids["flag_id"]) == 1
    assert _row_count(db, ClassGroup, id=other_ids["class_id"]) == 1
    assert _row_count(db, DataExport, school_id=other_school.id) >= 1


def test_append_only_triggers_still_enforced_after_a_hard_delete(
    client, db, make_school, make_staff, make_student
):
    """`hard_delete_school_cascade` disables the `flag_events` / `audit_logs` append-only triggers
    for exactly the two statements that need it, inside its own transaction. Prove the platform-
    wide guard is genuinely back on afterward — not a temporary hole any later mutation could slip
    through — by exercising it against a SECOND, completely unrelated school's data."""
    from src.domain.checkin.models import CheckIn as CheckInModel
    from src.domain.risk.models import Flag as FlagModel
    from src.domain.risk.models import FlagEvent as FlagEventModel

    school = make_school(code="TRIG-1")
    leader = make_staff(school, email="trig@oakwood.edu", role=StaffRole.leadership)
    make_student(school)
    token = _mint(db, leader)
    client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    r = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r.json()["deleted"] is True

    other_school = make_school(code="TRIG-2")
    other_leader = make_staff(other_school, email="trig2@oakwood.edu", role=StaffRole.leadership)
    other_student = make_student(other_school)
    checkin = CheckInModel(
        student_id=other_student.id, school_id=other_school.id, mood_value=2,
        local_date=date.today(),
    )
    db.add(checkin)
    db.flush()
    flag = FlagModel(
        student_id=other_student.id, school_id=other_school.id, checkin_id=checkin.id,
        type=FlagType.concern_word, risk_score=Decimal("0.5"),
    )
    db.add(flag)
    db.flush()
    event = FlagEventModel(flag_id=flag.id, event_type=FlagEventType.viewed, actor_id=other_leader.id)
    db.add(event)
    db.commit()

    with pytest.raises(sa.exc.ProgrammingError, match="append-only table"):
        db.execute(sa.delete(FlagEventModel).where(FlagEventModel.id == event.id))
        db.flush()
    db.rollback()

    audit = db.scalar(
        select(AuditLog).where(AuditLog.action == "fr_20_02.export_and_delete")
    )
    assert audit is not None
    with pytest.raises(sa.exc.ProgrammingError, match="append-only table"):
        db.execute(sa.update(AuditLog).where(AuditLog.id == audit.id).values(action="tampered"))
        db.flush()
    db.rollback()


def test_second_call_is_a_no_op_shaped_conflict_once_school_is_gone(
    client, db, make_school, make_staff, make_student
):
    """After the school is truly gone, the actor's own account is gone too — a THIRD call with the
    same (now-stale) token can no longer authenticate. This is the natural end-state, not a bug to
    special-case: nobody from a hard-deleted school can act again."""
    school = make_school(code="HD-3")
    leader = make_staff(school, email="lead6@oakwood.edu", role=StaffRole.leadership)
    make_student(school)
    token = _mint(db, leader)

    client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    r = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r.json()["deleted"] is True

    r3 = client.post(f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token))
    assert r3.status_code in (401, 403)


# ---- authorisation: leadership, own school only (GATE-12 shape) -----------------------------------

def test_non_leadership_staff_cannot_start_the_exit(client, make_school, make_staff):
    school = make_school(code="AUTHZ-1")
    make_staff(school, email="teacher@oakwood.edu", role=StaffRole.teacher, password="Password123")
    r = client.post(
        STAFF_SIGNIN, json={"email": "teacher@oakwood.edu", "password": "Password123"}
    )
    token = r.json()["access_token"]
    r2 = client.post(
        f"/api/v1/schools/{school.id}/export-and-delete", headers=_auth(token)
    )
    assert r2.status_code == 403


def test_leadership_cannot_delete_a_different_school(client, db, make_school, make_staff):
    school_a = make_school(code="AUTHZ-2")
    leader = make_staff(school_a, email="a@oakwood.edu", role=StaffRole.leadership)
    school_b = make_school(code="AUTHZ-3")
    token = _mint(db, leader)
    r = client.post(f"/api/v1/schools/{school_b.id}/export-and-delete", headers=_auth(token))
    assert r.status_code == 403
    # nothing touched on the targeted school either
    assert db.get(School, school_b.id) is not None


def test_role_check_runs_before_school_scope_check(db, make_school, make_staff):
    school_a = make_school(code="AUTHZ-4")
    other_school = make_school(code="AUTHZ-5")
    teacher = make_staff(school_a, email="t@oakwood.edu", role=StaffRole.teacher)

    with pytest.raises(HTTPException) as exc:
        deletion_svc.request_export_and_delete(db, teacher, other_school.id)
    assert exc.value.status_code == 403


# ---- concurrency: two genuinely concurrent "start the exit" calls for the SAME school -------------

def _race_start_exit(engine, staff_id: uuid.UUID, school_id: uuid.UUID, barrier, results, key) -> None:
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        staff = session.get(StaffAccount, staff_id)
        barrier.wait()
        try:
            deletion_svc.request_export_and_delete(session, staff, school_id)
            session.commit()
            results[key] = ("success", None)
        except HTTPException as exc:
            session.rollback()
            results[key] = ("http_exception", exc.status_code)
    except Exception as exc:  # noqa: BLE001 — any unhandled exception here IS the failure mode
        session.rollback()
        results[key] = ("unhandled_exception", repr(exc))
    finally:
        session.close()


def test_concurrent_exit_starts_never_double_insert(db, make_school, make_staff):
    """Two genuinely concurrent callers both trying to start the SAME school's exit must not both
    create an export_and_delete row (uq_data_exports_school_export_and_delete backstops the
    application's own read-then-write) — exactly one succeeds, the loser converges on the SAME 409
    the sequential ordering guard gives, neither raises unhandled."""
    school = make_school(code="RACE-1")
    leader = make_staff(school, email="racer@oakwood.edu", role=StaffRole.leadership)

    engine = create_engine(env_settings.database_url_test, future=True, pool_size=5, max_overflow=5)
    try:
        barrier = threading.Barrier(2)
        results: dict = {}
        t_a = threading.Thread(
            target=_race_start_exit, args=(engine, leader.id, school.id, barrier, results, "a")
        )
        t_b = threading.Thread(
            target=_race_start_exit, args=(engine, leader.id, school.id, barrier, results, "b")
        )
        t_a.start()
        t_b.start()
        t_a.join(timeout=30)
        t_b.join(timeout=30)

        assert set(results) == {"a", "b"}, f"a thread never finished: {results}"
        kinds = sorted(v[0] for v in results.values())
        assert kinds == ["http_exception", "success"], (
            f"expected exactly one winner and one 409 loser, got {results} — an "
            f"'unhandled_exception' means the race is NOT closed"
        )
        loser = next(v for v in results.values() if v[0] == "http_exception")
        assert loser[1] == 409

        count = db.execute(
            sa.text(
                "SELECT COUNT(*) FROM data_exports WHERE school_id = :sid "
                "AND kind = 'export_and_delete'"
            ),
            {"sid": str(school.id)},
        ).scalar()
        assert count == 1, f"expected exactly one export_and_delete row, found {count}"
    finally:
        engine.dispose()
