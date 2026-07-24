"""FR-03-01: DB backstop for roster-import idempotency on retry.

One partial unique index that the application select-then-write (`get_student_by_external_ref`)
cannot provide on its own:

* ``uq_students_school_external_ref`` — at most one student per (school, external_ref) where
  external_ref is set, so two genuinely concurrent roster-import retries carrying the same
  school-supplied student id cannot both INSERT. Rows with no external_ref (no natural key) are
  excluded on purpose — those are documented as always-CREATE (see
  ``src.application.roster.services``).

Mirrors ``uq_schools_name_live`` (e1c7b40a9d38, FR-02-01) and the `ix_subscriptions_school_id`
uniqueness fix (f4a9c1e7b382, FR-19-02 review) — the same class of race, closed the same way.

Revision ID: a1b2c3d4e5f6
Revises: f4a9c1e7b382
Create Date: 2026-07-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "f4a9c1e7b382"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_students_school_external_ref ON students (school_id, external_ref) "
        "WHERE external_ref IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_students_school_external_ref")
