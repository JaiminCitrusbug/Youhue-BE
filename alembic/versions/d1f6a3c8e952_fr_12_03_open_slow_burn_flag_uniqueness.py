"""FR-12-03: DB-level backstop for slow-burn evaluate idempotency. `evaluate_slow_burn` always
inserts `checkin_id=NULL` (a background evaluation is not anchored to one check-in), so the
existing `uq_flags_checkin_id` constraint gives zero protection (Postgres treats every NULL as
distinct under a standard UNIQUE constraint) - review Blocker, proven by execution (2 duplicate
open slow_burn flags on the very first of 8 concurrent-evaluate iterations). Fix: a partial unique
index over OPEN flags per (student_id, type) - the same `postgresql_where` shape FR-19-05 already
used for its single-default-row constraint.

Single alembic head (chains off FR-12-01, which added no migration). Tests build the schema via
this real chain, so model/migration drift fails the suite.

Revision ID: d1f6a3c8e952
Revises: c4d8f2a91b7e
Create Date: 2026-07-25 18:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f6a3c8e952"
down_revision: Union[str, None] = "a3f0c9d21e77"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # At most one OPEN flag per (student_id, type) -> two concurrent slow-burn evaluations for the
    # same student can no longer both insert; the loser's INSERT raises IntegrityError, caught and
    # converged in application code. Closed/acknowledged/escalated/actioned flags are excluded, so
    # a student can still accrue new flags of the same type once a prior one is resolved.
    op.create_index(
        "uq_flags_open_student_type",
        "flags",
        ["student_id", "type"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("uq_flags_open_student_type", table_name="flags")
