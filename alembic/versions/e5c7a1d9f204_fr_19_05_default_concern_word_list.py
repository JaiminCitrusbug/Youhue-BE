"""FR-19-05: platform default concern-word list — server_default on is_default + a single-default
partial unique index (at most one is_default=true row). The internal team maintains this default;
INFRA-06 scoring falls back to it when a school has no override (GATE G-6 keeps overrides untouched).

Single alembic head (chains off FR-19-01). Tests build the schema via this real chain, so
model/migration drift fails the suite.

Revision ID: e5c7a1d9f204
Revises: b3d9f1a4c7e2
Create Date: 2026-07-23 09:15:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e5c7a1d9f204"
down_revision: Union[str, None] = "e1c7b40a9d38"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default so existing rows keep is_default=false and the flag has a DB-level default.
    op.alter_column(
        "concern_word_lists",
        "is_default",
        existing_type=sa.Boolean(),
        server_default=sa.text("false"),
        existing_nullable=False,
    )
    # At most ONE platform default row -> a partial unique index over is_default=true rows makes the
    # default a single, upsert-idempotent record (school override rows, is_default=false, excluded).
    op.create_index(
        "uq_concern_word_lists_default",
        "concern_word_lists",
        ["is_default"],
        unique=True,
        postgresql_where=sa.text("is_default"),
    )


def downgrade() -> None:
    op.drop_index("uq_concern_word_lists_default", table_name="concern_word_lists")
    op.alter_column(
        "concern_word_lists",
        "is_default",
        existing_type=sa.Boolean(),
        server_default=None,
        existing_nullable=False,
    )
