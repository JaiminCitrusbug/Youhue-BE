"""FR-04-01 review remediation (Blocker 1, `docs/reviews/FR-04-01.md` Finding 1): DB backstop for
the "one check-in per student per school-local day" invariant.

The application already enforces this with a select-then-insert
(`src.application.checkin.services.submit_checkin`) — proven correct SEQUENTIALLY (exact retry ->
idempotent 201, different content -> 409) but, per the review's own concurrent-session reproduction
(real per-request session pool, not the shared test-client session), a genuine concurrent second
request wins the race 8/10 times and inserts a SECOND `CheckIn` row for the same student/day, both
returning 201. Every comparable read-then-write invariant elsewhere in this codebase closed the same
race with a dedicated unique-index migration after review (`e1c7b40a9d38` FR-02-01,
`a1b2c3d4e5f6` FR-03-01, `f4a9c1e7b382` FR-19-02, `f20606a1b2c3` FR-20-06) — this follows the
FR-02-01/FR-03-01 shape (a plain composite unique constraint + application-side `IntegrityError` ->
409 translation), the closest fit: check-in semantics want a hard reject of a genuine duplicate
(matching the ALREADY-CORRECT sequential-different-content -> 409 behavior), not a silent merge/
convergence onto the winner's row (the FR-19-02 upsert+lock shape) — a check-in's content (mood,
reflection) is a student's deliberate choice, not a resource whose winner is interchangeable.

`check_ins.local_date` (new column) is the school-LOCAL calendar day the check-in was submitted on,
computed by the application from `CalendarConfig.timezone` the same way `submit_checkin`'s existing
day-boundary read already does. It cannot be a functional index on `submitted_at` alone:
`submitted_at` is a UTC timestamptz and the local day depends on a per-school timezone that lives in
a different table — not expressible in a single-table Postgres expression index. The column is
added nullable, best-effort-backfilled from `submitted_at::date` (a school running purely in UTC —
the only rows that could exist before this ticket's write path shipped — needs no correction; any
non-UTC-school row would need re-deriving, but the check-in domain's first writer is this ticket, so
no such row can exist yet), then tightened to NOT NULL before the constraint goes on.

Revision ID: c4d8f2a91b7e
Revises: b3f04a01c4e2
Create Date: 2026-07-24 15:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d8f2a91b7e"
down_revision: Union[str, None] = "b3f04a01c4e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("check_ins", sa.Column("local_date", sa.Date(), nullable=True))
    op.execute(
        "UPDATE check_ins SET local_date = (submitted_at AT TIME ZONE 'UTC')::date "
        "WHERE local_date IS NULL"
    )
    op.alter_column("check_ins", "local_date", nullable=False)
    op.create_unique_constraint(
        "uq_checkins_student_local_date", "check_ins", ["student_id", "local_date"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_checkins_student_local_date", "check_ins", type_="unique")
    op.drop_column("check_ins", "local_date")
