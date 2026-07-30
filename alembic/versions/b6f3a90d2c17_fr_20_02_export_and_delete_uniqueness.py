"""FR-20-02 (SC-065, ticket DoD): at most ONE `export_and_delete`-kind `DataExport` row per school,
ever — the school-exit flow's own idempotency/race backstop (BR-05), the same `postgresql_where`
partial-unique-index shape already used for `uq_flags_open_student_type` (FR-12-03) and
`uq_concern_word_lists_default` (FR-19-05). Two concurrent "start the exit" calls for the same
school must not both insert a fresh row — a race the sequential ordering guard (409 before the
export is ready) does not by itself prevent.

Revision ID: b6f3a90d2c17
Revises: c2a6f0e91d53
Create Date: 2026-07-30 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b6f3a90d2c17"
down_revision: Union[str, None] = "c2a6f0e91d53"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_data_exports_school_export_and_delete",
        "data_exports",
        ["school_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'export_and_delete'"),
    )


def downgrade() -> None:
    op.drop_index("uq_data_exports_school_export_and_delete", table_name="data_exports")
