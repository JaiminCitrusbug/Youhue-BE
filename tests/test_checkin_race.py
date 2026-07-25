"""FR-04-01 review remediation (`docs/reviews/FR-04-01.md` Finding 1 — Blocker): a concurrency
regression test for the "one check-in per student per day" invariant, using GENUINELY independent
DB sessions (each its own connection off a real pooled engine — `config/db_connection.py`'s own
`SessionLocal` shape), not the shared single-session `client`/`db` fixture other `test_checkin_*`
files use. The review's own finding is explicit about why that distinction matters: the shared test
-client session is exactly what let the original race hide (`tests/conftest.py`'s `client` fixture
holds ONE `db` session for the whole test, so two "concurrent" `client.post()` calls through it are
never truly concurrent at the database level).

Reproduces the reviewer's own methodology (real per-request session pool, two threads racing to
submit DIFFERENT `mood_value`s for the same student/day, synchronized with a `Barrier` to maximize
interleaving) for >=10 iterations and asserts, every time:
  - exactly one thread's `submit_checkin` call succeeds (a `CheckIn` row is created),
  - the other gets a clean `HTTPException(409)` — never a bare `IntegrityError`, never a 500,
  - exactly ONE `CheckIn` row exists in the DB for that student afterward.
"""
import threading
import uuid
from datetime import time

from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config.env_config import settings
from src.application.checkin import services as checkin_svc
from src.constants.enums import ParentalConsentStatus
from src.domain.compliance import services as compliance_db
from src.domain.identity.models import Student
from src.domain.org.models import CalendarConfig

_ITERATIONS = 10


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


def _race_attempt(
    engine, student_id: uuid.UUID, mood_value: int, barrier: threading.Barrier,
    results: dict, key: str,
) -> None:
    """Runs in its own thread with its OWN session/connection — the real-concurrency shape the
    review used, not the shared test-client session."""
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        student = session.get(Student, student_id)
        barrier.wait()  # line both threads up at the same starting line
        _row, _created = checkin_svc.submit_checkin(session, student, mood_value, None)
        session.commit()
        results[key] = ("success", None)
    except HTTPException as exc:
        session.rollback()
        results[key] = ("http_exception", exc.status_code)
    except Exception as exc:  # noqa: BLE001 — a bare IntegrityError leaking here IS the failure mode
        session.rollback()
        results[key] = ("unhandled_exception", repr(exc))
    finally:
        session.close()


def test_concurrent_different_mood_submissions_never_double_write(
    db, make_school, make_student
):
    """The reviewer's exact reproduction: two genuinely concurrent POST-equivalents, different
    moods, same student/day. Run >=10 times (review methodology: 8/10 trials produced two rows
    before this fix); every iteration here must land exactly one row and one clean 409."""
    school = make_school(code="RACE-1")
    _open_all_day_window(db, school)

    engine = create_engine(settings.database_url_test, future=True, pool_size=5, max_overflow=5)
    try:
        outcomes = []
        for i in range(_ITERATIONS):
            student = make_student(school, name=f"Racer{i}")
            _verify_consent(db, student)

            barrier = threading.Barrier(2)
            results: dict = {}
            t_a = threading.Thread(
                target=_race_attempt, args=(engine, student.id, 4, barrier, results, "a")
            )
            t_b = threading.Thread(
                target=_race_attempt, args=(engine, student.id, 1, barrier, results, "b")
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=30)
            t_b.join(timeout=30)

            assert set(results) == {"a", "b"}, f"iteration {i}: a thread never finished: {results}"
            outcomes.append(results)

            kinds = sorted(v[0] for v in results.values())
            assert kinds == ["http_exception", "success"], (
                f"iteration {i}: expected exactly one success + one clean http_exception, "
                f"got {results} — an 'unhandled_exception' here means a raw IntegrityError leaked "
                f"past the fix instead of being translated to 409"
            )
            rejected = [v for v in results.values() if v[0] == "http_exception"]
            assert rejected[0][1] == 409, f"iteration {i}: loser must get 409, got {rejected}"

            count = db.execute(
                text("SELECT COUNT(*) FROM check_ins WHERE student_id = :sid"),
                {"sid": str(student.id)},
            ).scalar()
            assert count == 1, (
                f"iteration {i}: expected exactly one CheckIn row for student {student.id}, "
                f"found {count} — the race is NOT closed"
            )

        # Sanity: every one of the >=10 trials produced a clean 201-vs-409 split, not a 201-vs-201
        # (the pre-fix failure mode the review measured 8/10 times).
        assert len(outcomes) == _ITERATIONS
    finally:
        engine.dispose()


def test_sequential_exact_retry_still_idempotent_201(client, db, make_school, make_student):
    """Unregressed (review-confirmed-correct behavior, re-verified after the fix): an exact-content
    retry still returns the SAME 201 / SAME row, never a 409 from the new constraint."""
    from src.domain.checkin.models import CheckIn

    school = make_school(code="RACE-2")
    student = make_student(school)
    _open_all_day_window(db, school)
    _verify_consent(db, student)
    token_resp = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    token = token_resp.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}
    body = {"mood_value": 4, "reflection_text": "same"}

    first = client.post("/api/v1/check-ins", json=body, headers=headers)
    retry = client.post("/api/v1/check-ins", json=body, headers=headers)

    assert first.status_code == 201
    assert retry.status_code == 201
    assert first.json()["checkin_id"] == retry.json()["checkin_id"]
    assert db.query(CheckIn).filter(CheckIn.student_id == student.id).count() == 1


def _sync_race_attempt(
    engine, student_id: uuid.UUID, client_entry_id: str, barrier: threading.Barrier,
    results: dict, key: str,
) -> None:
    """FR-04-06 — same real-concurrency shape as `_race_attempt` above, but racing two
    `sync_checkin` calls that carry the SAME `client_entry_id` (e.g. a background-sync event and a
    foreground reconnect handler both firing at once for the same retained offline entry)."""
    from src.application.checkin import services as sync_svc

    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        student = session.get(Student, student_id)
        barrier.wait()
        row, created = sync_svc.sync_checkin(session, student, client_entry_id, 4, None)
        session.commit()
        results[key] = ("success", (row.id, created))
    except HTTPException as exc:
        session.rollback()
        results[key] = ("http_exception", exc.status_code)
    except Exception as exc:  # noqa: BLE001 — a bare IntegrityError leaking here IS the failure mode
        session.rollback()
        results[key] = ("unhandled_exception", repr(exc))
    finally:
        session.close()


def test_concurrent_sync_same_entry_id_never_double_creates(db, make_school, make_student):
    """FR-04-06 review Blocker (`docs/reviews/FR-12-03.md`-class race, 6th instance this codebase
    has hit): two genuinely concurrent syncs of the SAME `client_entry_id` must converge onto ONE
    row, never two, and never a raw `IntegrityError`. Run >=10 times."""
    school = make_school(code="RACE-SYNC-1")
    _open_all_day_window(db, school)

    engine = create_engine(settings.database_url_test, future=True, pool_size=5, max_overflow=5)
    try:
        for i in range(_ITERATIONS):
            student = make_student(school, name=f"SyncRacer{i}")
            _verify_consent(db, student)
            entry_id = f"race-entry-{i}"

            barrier = threading.Barrier(2)
            results: dict = {}
            t_a = threading.Thread(
                target=_sync_race_attempt,
                args=(engine, student.id, entry_id, barrier, results, "a"),
            )
            t_b = threading.Thread(
                target=_sync_race_attempt,
                args=(engine, student.id, entry_id, barrier, results, "b"),
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=30)
            t_b.join(timeout=30)

            assert set(results) == {"a", "b"}, f"iteration {i}: a thread never finished: {results}"
            kinds = [v[0] for v in results.values()]
            assert kinds == ["success", "success"], (
                f"iteration {i}: both racers must converge to success (the idempotency key means "
                f"neither is a 'genuine duplicate' to reject) — got {results}. An "
                f"'unhandled_exception' means a raw IntegrityError leaked past the fix."
            )

            row_ids = {v[1][0] for v in results.values()}
            assert len(row_ids) == 1, (
                f"iteration {i}: both racers must resolve to the SAME row id, got {row_ids} — "
                f"the race produced two rows for one client_entry_id"
            )

            count = db.execute(
                text("SELECT COUNT(*) FROM check_ins WHERE student_id = :sid"),
                {"sid": str(student.id)},
            ).scalar()
            assert count == 1, (
                f"iteration {i}: expected exactly one CheckIn row for student {student.id}, "
                f"found {count} — the client_entry_id race is NOT closed"
            )
    finally:
        engine.dispose()


def test_sequential_different_mood_still_409_original_preserved(
    client, db, make_school, make_student
):
    """Unregressed (review-confirmed-correct behavior, re-verified after the fix): a sequential
    different-mood second submission still 409s and the original row's content survives untouched."""
    from src.domain.checkin.models import CheckIn

    school = make_school(code="RACE-3")
    student = make_student(school)
    _open_all_day_window(db, school)
    _verify_consent(db, student)
    token_resp = client.post(
        "/api/v1/auth/student/sign-in",
        json={"school_or_class_code": school.sign_in_code, "student_id": str(student.id)},
    )
    token = token_resp.json()["session_token"]
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post("/api/v1/check-ins", json={"mood_value": 4}, headers=headers)
    assert first.status_code == 201
    second = client.post("/api/v1/check-ins", json={"mood_value": 1}, headers=headers)
    assert second.status_code == 409

    rows = db.query(CheckIn).filter(CheckIn.student_id == student.id).all()
    assert len(rows) == 1
    assert rows[0].mood_value == 4
