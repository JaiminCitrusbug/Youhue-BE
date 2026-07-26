"""FR-02-04: Invitation.previous_token — recognise a superseded (resent) link specifically,
instead of a generic invalid-token bucket (ticket Scenario 3).

Revision ID: b9d2e6f1a3c5
Revises: f7a3c9e21b04
Create Date: 2026-07-27 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9d2e6f1a3c5"
down_revision: Union[str, None] = "f7a3c9e21b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("invitations", sa.Column("previous_token", sa.String(), nullable=True))
    op.create_index(
        "ix_invitations_previous_token", "invitations", ["previous_token"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_invitations_previous_token", table_name="invitations")
    op.drop_column("invitations", "previous_token")
