"""FR-12-05 (alert recipient config, ticket DoD): closes a KNOWN GAP FR-16-02 explicitly left
(`src/domain/risk/services.py::set_alert_recipient_config` docstring): "there is no DB-level unique
constraint on (school_id, alert_type) — this read-then-write is an application-level upsert, not a
DB-enforced one... a real unique index is a schema change left for FR-12-05 if it needs a stronger
guarantee." FR-12-05's own dedicated write path needs exactly that guarantee (a real `PUT` endpoint
callable concurrently, unlike FR-16-02's low-concurrency leadership-edit surface), so this adds the
unique index and the write path is upgraded to a real `INSERT ... ON CONFLICT ... DO UPDATE` upsert —
same concurrency-hardening precedent as FR-19-02/FR-12-03's fixes this session.

Revision ID: f7a3c9e21b04
Revises: d1e9a4b73f02
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a3c9e21b04"
down_revision: Union[str, None] = "d1e9a4b73f02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "uq_alert_recipient_configs_school_type",
        "alert_recipient_configs",
        ["school_id", "alert_type"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_alert_recipient_configs_school_type", table_name="alert_recipient_configs")
