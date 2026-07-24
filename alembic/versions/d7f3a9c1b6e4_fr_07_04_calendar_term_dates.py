"""FR-07-04: minimal term-dates surface on CalendarConfig.

FR-16-02 (SC-063) covers window_start/window_end/timezone only; the ticket confirmed no
term-dates storage exists anywhere in the model. Rather than a new table, this adds two
nullable columns to the SAME ``calendar_configs`` row FR-16-02 already owns — 'this term'
resolution (FR-07-04) reads them; the SC-063 settings hub is extended to write them.

Revision ID: d7f3a9c1b6e4
Revises: a1b2c3d4e5f6
Create Date: 2026-07-24 11:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7f3a9c1b6e4"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("calendar_configs", sa.Column("term_start", sa.Date(), nullable=True))
    op.add_column("calendar_configs", sa.Column("term_end", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("calendar_configs", "term_end")
    op.drop_column("calendar_configs", "term_start")
