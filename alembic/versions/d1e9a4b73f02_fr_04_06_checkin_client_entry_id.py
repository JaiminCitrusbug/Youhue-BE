"""FR-04-06 (offline check-in sync, ticket DoD): "Data model: CheckIn.captured_offline (BOOLEAN),
CheckIn.client_entry_id (VARCHAR, UNIQUE, indexed) added." `captured_offline` already existed
(INFRA-02's original model) — this migration only adds the new `client_entry_id` column, the
idempotency key for `POST /api/v1/check-ins/sync`. Nullable: only offline-originated check-ins carry
one; the normal online `POST /api/v1/check-ins` path never sets it. A plain (non-partial) UNIQUE
constraint is correct here — Postgres treats every NULL as distinct, so any number of ordinary
online check-ins (client_entry_id IS NULL) coexist safely; only actual client_entry_id VALUES are
required to be unique.

Revision ID: d1e9a4b73f02
Revises: a3f0c9d21e77
Create Date: 2026-07-25 19:10:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1e9a4b73f02"
down_revision: Union[str, None] = "d1f6a3c8e952"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "check_ins", sa.Column("client_entry_id", sa.String(), nullable=True)
    )
    op.create_index(
        "ix_checkins_client_entry_id",
        "check_ins",
        ["client_entry_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_checkins_client_entry_id", table_name="check_ins")
    op.drop_column("check_ins", "client_entry_id")
