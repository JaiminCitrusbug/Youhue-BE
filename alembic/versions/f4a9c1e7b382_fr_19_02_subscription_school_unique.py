"""FR-19-02 fix (fresh-eyes review Blocker 1): DB-level unique constraint on
subscriptions.school_id.

`ix_subscriptions_school_id` (migration 8ab7f8057e0a) was created `unique=False`. Combined with
`get_or_create_subscription`'s plain select-then-insert, two concurrent first-touch
`extend_trial` requests on the same never-extended school could both observe "no subscription row
yet" and both insert one, each independently marked "extended once" — defeating the ticket's one
and only hard guarantee (one 14-day extension per school, ever). Proven live by the fresh-eyes
review (`docs/reviews/FR-19-02.md` Finding 1).

This migration replaces the non-unique index with a unique one on the SAME column (no redundant
index kept). It is the shared DB-level backstop for EVERY caller of `get_or_create_subscription` —
including FR-02-02's school-approval first-writer (a sibling lane on a separate branch) — not just
this ticket's `extend_trial` path.

Revision ID: f4a9c1e7b382
Revises: f20606a1b2c3
Create Date: 2026-07-24 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a9c1e7b382"
down_revision: Union[str, None] = "f20606a1b2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_subscriptions_school_id", table_name="subscriptions")
    op.create_index(
        "ix_subscriptions_school_id", "subscriptions", ["school_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_subscriptions_school_id", table_name="subscriptions")
    op.create_index(
        "ix_subscriptions_school_id", "subscriptions", ["school_id"], unique=False
    )
