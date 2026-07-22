"""FR-01-02 student sign-in: per-class join code + qr_token, session qr_token replay marker

Revision ID: d4e5f6a1b2c3
Revises: c0ffee000001
Create Date: 2026-07-22 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a1b2c3'
down_revision: Union[str, None] = 'c0ffee000001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Persistent per-class sign-in credentials (rotate-only, no auto-expiry). Nullable: a class has
    # none until first issued. server_default NULL keeps the ADD COLUMN safe on non-empty tables.
    op.add_column(
        'class_groups',
        sa.Column('join_code', sa.String(), nullable=True, server_default=sa.null()),
    )
    op.add_column(
        'class_groups',
        sa.Column('qr_token', sa.String(), nullable=True, server_default=sa.null()),
    )
    op.create_unique_constraint('uq_class_groups_join_code', 'class_groups', ['join_code'])
    op.create_unique_constraint('uq_class_groups_qr_token', 'class_groups', ['qr_token'])
    # The class qr_token a student-session was established from (per-session single-use replay
    # marker). Nullable — null for code / staff / admin sign-ins.
    op.add_column(
        'sessions',
        sa.Column('qr_token', sa.String(), nullable=True, server_default=sa.null()),
    )


def downgrade() -> None:
    op.drop_column('sessions', 'qr_token')
    op.drop_constraint('uq_class_groups_qr_token', 'class_groups', type_='unique')
    op.drop_constraint('uq_class_groups_join_code', 'class_groups', type_='unique')
    op.drop_column('class_groups', 'qr_token')
    op.drop_column('class_groups', 'join_code')
