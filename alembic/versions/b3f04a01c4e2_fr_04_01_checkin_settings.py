"""FR-04-01: checkin_settings table (school-level "require a reflection" toggle, ticket Scenario 3).

The base `check_ins` table (id, mood_value, reflection_text, submitted_at, within_window, ...)
already exists — created by INFRA-02 (`8ab7f8057e0a`), confirmed by FR-07-03's own review — so this
migration does NOT touch it. A "require reflection" school setting does not exist anywhere yet
(FR-16-02's settings hub only covers concern-words/alert-routing/access-window); this adds the
minimal new row FR-04-01 needs, in the check-in domain rather than piling an unrelated boolean onto
`calendar_configs`.

Revision ID: b3f04a01c4e2
Revises: d7f3a9c1b6e4
Create Date: 2026-07-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f04a01c4e2"
down_revision: Union[str, None] = "d7f3a9c1b6e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # `unique=True, index=True` on the model's school_id -> ONE unique index, no separate
    # UniqueConstraint (mirrors `ix_subscriptions_school_id`, `f4a9c1e7b382`).
    op.create_table(
        "checkin_settings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("school_id", sa.Uuid(), nullable=False),
        sa.Column(
            "require_reflection", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.ForeignKeyConstraint(["school_id"], ["schools.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_checkin_settings_school_id", "checkin_settings", ["school_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_checkin_settings_school_id", table_name="checkin_settings")
    op.drop_table("checkin_settings")
