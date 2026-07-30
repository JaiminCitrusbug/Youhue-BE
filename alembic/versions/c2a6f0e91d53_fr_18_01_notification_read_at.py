"""FR-18-01 (SC-054 notifications centre): adds `notifications.read_at` — nullable, null = unread.
Set once by the new mark-all-read endpoint. Per-notification (not per-channel) read state; distinct
from `alert_deliveries` which tracks the per-channel SEND lifecycle, not the recipient's read state.

Revision ID: c2a6f0e91d53
Revises: a7e9c1f34b56
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2a6f0e91d53"
down_revision: Union[str, None] = "a7e9c1f34b56"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "notifications", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("notifications", "read_at")
