"""FR-17-04 downgrade-sweep worker entrypoint — mirrors `application/notifications/worker.py`'s
established shape (INFRA-05/FR-18-03): one pass over due subscriptions and exit, no always-on
daemon loop. Invoked on demand / by an external scheduler (cron, k8s CronJob):

    python -m src.application.billing.worker

`run_once` is importable and unit-tested; `main` wires a session, commits, and logs the count.
"""
import logging

from config.db_connection import SessionLocal
from src.application.billing.downgrade import run_downgrade_sweep

logger = logging.getLogger("youhue.billing")


def run_once() -> int:
    """One sweep pass in its own session+transaction. Returns the count of schools downgraded."""
    db = SessionLocal()
    try:
        downgraded = run_downgrade_sweep(db)
        db.commit()
        return len(downgraded)
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    downgraded = run_once()
    logger.info("fr_17_04_success action=sweep_pass_complete downgraded=%s", downgraded)


if __name__ == "__main__":
    main()
