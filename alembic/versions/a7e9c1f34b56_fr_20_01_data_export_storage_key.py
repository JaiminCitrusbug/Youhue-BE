"""FR-20-01 (school data export, ticket DoD): `data_exports` already exists (INFRA-02,
8ab7f8057e0a — id/school_id/requested_by/kind/status/created_at) but has nowhere to record WHERE the
finished artifact was written in object storage. Adds `storage_key` (nullable — unset while
`status=pending`, set the moment the artifact is durably written and `status` flips to `ready`).

Revision ID: a7e9c1f34b56
Revises: e7c1a9f4b620
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7e9c1f34b56"
down_revision: Union[str, None] = "e7c1a9f4b620"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("data_exports", sa.Column("storage_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("data_exports", "storage_key")
