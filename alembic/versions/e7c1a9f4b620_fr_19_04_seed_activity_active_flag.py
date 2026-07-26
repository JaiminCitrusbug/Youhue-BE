"""FR-19-04 admin seed activities: activities.active (retire flag)

Adds a boolean `active` column to `activities` so the internal team can retire a seed activity
(soft-deactivate) without deleting engagement history. The column carries a server_default of TRUE,
so the ADD COLUMN is safe on a non-empty table and every existing activity backfills as active.
Single alembic head (chains off the FR-19-01 admin-role migration); tests build the schema via this
real chain, so model/migration drift fails the suite.

Revision ID: e7c1a9f4b620
Revises: b9d2e6f1a3c5
Create Date: 2026-07-23 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7c1a9f4b620"
down_revision: Union[str, None] = "b9d2e6f1a3c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "activities",
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("activities", "active")
