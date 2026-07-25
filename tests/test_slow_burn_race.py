"""FR-12-03 review remediation (`docs/reviews/FR-12-03.md` Blocker 1): a concurrency regression
test for `evaluate_slow_burn`'s idempotency, using GENUINELY independent DB sessions (each its own
connection off a real pooled engine), not the shared single-session `db` fixture — same shape and
rationale as `tests/test_checkin_race.py` (FR-04-01's own race-fix regression test): the shared
test-client session is exactly what would hide this race.

Reproduces the reviewer's own methodology (real per-request session pool, two threads racing
`risk_svc.evaluate_slow_burn` for the SAME student whose history already clears the slow-burn
threshold, synchronized with a `Barrier` to maximize interleaving) for >=10 iterations and asserts,
every time:
  - BOTH threads return `flag_raised=True` (this is upsert-and-converge, not reject-the-loser — a
    re-evaluation racing with itself should converge onto the winner's flag, never error),
  - exactly ONE `slow_burn` Flag row exists for that student afterward (review's pre-fix
    reproduction: 2 rows on the very first of 8 iterations).
"""
import threading
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from config.env_config import settings
from src.application.risk import services as risk_svc
from src.domain.checkin.models import CheckIn

_ITERATIONS = 10


def _mk_low_history(db, student, school) -> None:
    """Enough low check-ins to clear the slow-burn threshold, mirroring test_risk.py's own
    `_mk_checkin` shape but written directly here to keep this file self-contained."""
    for i in range(settings.slowburn_window_days):
        row = CheckIn(
            student_id=student.id,
            school_id=school.id,
            mood_value=1,
            reflection_text=None,
            submitted_at=datetime.now(UTC) - timedelta(days=i),
            local_date=(datetime.now(UTC) - timedelta(days=i)).date(),
            scored=True,
        )
        db.add(row)
    db.commit()


def _race_attempt(
    engine, student_id: uuid.UUID, school_id: uuid.UUID, barrier: threading.Barrier,
    results: dict, key: str,
) -> None:
    """Runs in its own thread with its OWN session/connection — the real-concurrency shape the
    review used, not the shared test-client session."""
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        barrier.wait()  # line both threads up at the same starting line
        flag_raised = risk_svc.evaluate_slow_burn(session, student_id, school_id)
        session.commit()
        results[key] = ("success", flag_raised)
    except Exception as exc:  # noqa: BLE001 — any unhandled exception here IS the failure mode
        session.rollback()
        results[key] = ("unhandled_exception", repr(exc))
    finally:
        session.close()


def test_concurrent_evaluate_never_double_flags(db, make_school, make_student):
    """The reviewer's exact reproduction: two genuinely concurrent slow-burn evaluations for the
    same student whose history already clears the threshold. Run >=10 times (review methodology:
    duplicated on the very first of 8 trials before this fix); every iteration here must converge
    on exactly one open slow_burn flag, both callers reporting flag_raised=True."""
    school = make_school(code="SBRACE-1")

    engine = create_engine(settings.database_url_test, future=True, pool_size=5, max_overflow=5)
    try:
        for i in range(_ITERATIONS):
            student = make_student(school, name=f"SlowBurnRacer{i}")
            _mk_low_history(db, student, school)

            barrier = threading.Barrier(2)
            results: dict = {}
            t_a = threading.Thread(
                target=_race_attempt, args=(engine, student.id, school.id, barrier, results, "a")
            )
            t_b = threading.Thread(
                target=_race_attempt, args=(engine, student.id, school.id, barrier, results, "b")
            )
            t_a.start()
            t_b.start()
            t_a.join(timeout=30)
            t_b.join(timeout=30)

            assert set(results) == {"a", "b"}, f"iteration {i}: a thread never finished: {results}"

            kinds = sorted(v[0] for v in results.values())
            assert kinds == ["success", "success"], (
                f"iteration {i}: expected both calls to converge cleanly, got {results} — an "
                f"'unhandled_exception' here means the DB-level backstop did not hold"
            )
            assert all(v[1] is True for v in results.values()), (
                f"iteration {i}: both concurrent evaluators must report flag_raised=True "
                f"(convergence), got {results}"
            )

            count = db.execute(
                text(
                    "SELECT COUNT(*) FROM flags WHERE student_id = :sid "
                    "AND type = 'slow_burn' AND status = 'open'"
                ),
                {"sid": str(student.id)},
            ).scalar()
            assert count == 1, (
                f"iteration {i}: expected exactly one open slow_burn flag for student "
                f"{student.id}, found {count} — the race is NOT closed"
            )
    finally:
        engine.dispose()


def test_sequential_evaluate_still_idempotent(db, make_school, make_student):
    """Unregressed (already covered in tests/test_risk.py at the HTTP level, re-verified here at
    the service level): two SEQUENTIAL calls for the same student converge on one flag, no error."""
    school = make_school(code="SBRACE-2")
    student = make_student(school)
    _mk_low_history(db, student, school)

    first = risk_svc.evaluate_slow_burn(db, student.id, school.id)
    db.commit()
    second = risk_svc.evaluate_slow_burn(db, student.id, school.id)
    db.commit()

    assert first is True and second is True
    count = db.execute(
        text(
            "SELECT COUNT(*) FROM flags WHERE student_id = :sid "
            "AND type = 'slow_burn' AND status = 'open'"
        ),
        {"sid": str(student.id)},
    ).scalar()
    assert count == 1
