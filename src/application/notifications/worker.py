"""Notification retry worker entrypoint (INFRA-05).

Runs ONE pass over due email deliveries and exits — no always-on daemon loop. The live scheduler
(a running daemon that loops on an interval) is DEFERRED until Wave-3 producers exist; see
DEFERRALS.md. Until then this is invoked on demand / by an external scheduler (cron, k8s CronJob):

    python -m src.application.notifications.worker

`run_once` is importable and unit-tested; `main` wires a session, commits, and logs the count.
"""
import logging

from config.db_connection import SessionLocal
from src.application.notifications.services import process_due_deliveries

logger = logging.getLogger("youhue.notifications")


def run_once() -> int:
    """One worker pass in its own session+transaction. Returns the count of deliveries processed."""
    db = SessionLocal()
    try:
        processed = process_due_deliveries(db)
        db.commit()
        return processed
    finally:
        db.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    processed = run_once()
    logger.info("fr_18_03_success action=worker_pass_complete processed=%s", processed)


if __name__ == "__main__":
    main()
