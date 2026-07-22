"""FR-19-01 admin console: internal_admins.role (internal-team RBAC role)

Adds the AdminRole enum type + a role column on internal_admins. The column carries a
server_default (least-privilege 'support') so the ADD COLUMN is safe on a non-empty table and
existing rows backfill deterministically. Single alembic head (chains off Lane A's class-access-codes
migration); tests build the schema via this real chain, so model/migration drift fails the suite.

Revision ID: b3d9f1a4c7e2
Revises: d4e5f6a1b2c3
Create Date: 2026-07-22 12:15:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d9f1a4c7e2"
down_revision: Union[str, None] = "d4e5f6a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ADMIN_ROLE = sa.Enum("superadmin", "support", name="admin_role")


def upgrade() -> None:
    _ADMIN_ROLE.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "internal_admins",
        sa.Column(
            "role",
            _ADMIN_ROLE,
            nullable=False,
            server_default="support",
        ),
    )


def downgrade() -> None:
    op.drop_column("internal_admins", "role")
    _ADMIN_ROLE.drop(op.get_bind(), checkfirst=True)
