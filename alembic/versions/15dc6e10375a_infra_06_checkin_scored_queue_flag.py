"""INFRA-06 risk pipeline schema: checkin scoring queue + retry counter, unrouted flag band,
one-flag-per-checkin idempotency.

Revision ID: 15dc6e10375a
Revises: 1aeee1926316
Create Date: 2026-07-22 10:40:32.045040

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '15dc6e10375a'
down_revision: Union[str, None] = '1aeee1926316'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # scoring queue flag + bounded-retry counter. server_default so the ADD COLUMN is safe on a
    # non-empty check_ins table (existing rows backfill to not-yet-scored / zero attempts).
    op.add_column(
        'check_ins',
        sa.Column('scored', sa.Boolean(), nullable=False, server_default=sa.text('false')),
    )
    op.add_column(
        'check_ins',
        sa.Column('score_attempts', sa.SmallInteger(), nullable=False, server_default=sa.text('0')),
    )
    # the action band is decided by FR-12-06 routing, not this pipeline -> nullable (null = unrouted).
    op.alter_column('flags', 'band', existing_type=sa.Enum(name='flag_band'), nullable=True)
    # one flag per scored check-in -> scoring is idempotent on retry.
    op.create_unique_constraint('uq_flags_checkin_id', 'flags', ['checkin_id'])


def downgrade() -> None:
    op.drop_constraint('uq_flags_checkin_id', 'flags', type_='unique')
    op.alter_column('flags', 'band', existing_type=sa.Enum(name='flag_band'), nullable=False)
    op.drop_column('check_ins', 'score_attempts')
    op.drop_column('check_ins', 'scored')
