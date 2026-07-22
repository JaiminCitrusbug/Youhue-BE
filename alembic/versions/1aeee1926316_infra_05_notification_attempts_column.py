"""INFRA-05 notification transport: move delivery state onto alert_deliveries

Repurposed (was: add notifications.attempts). Delivery lifecycle/attempts/backoff/flag_id belong on
the canonical alert_deliveries table, not on notifications. This migration:
  - strips channel + delivery_status from notifications (it is now the message only);
  - adds alert_deliveries.notification_id (FK -> notifications.id, NOT NULL, indexed);
  - adds alert_deliveries.next_attempt_at (backoff clock);
  - makes alert_deliveries.flag_id nullable (invites/transactional notifications have no flag).

Safety: alert_deliveries has no producer yet (Wave-3, deferred), so it is empty here. notification_id
is added nullable then promoted to NOT NULL (the safe idiom — a UUID FK cannot carry a server_default
without violating referential integrity). Any scalar NOT NULL add would carry a server_default.

Revision ID: 1aeee1926316
Revises: 8ab7f8057e0a
Create Date: 2026-07-22 10:34:31.093966

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '1aeee1926316'
down_revision: Union[str, None] = '8ab7f8057e0a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# shared enum types already created by INFRA-02 (create_type=False -> never re-create).
_ALERT_CHANNEL = postgresql.ENUM('email', 'in_app', name='alert_channel', create_type=False)
_DELIVERY_STATUS = postgresql.ENUM(
    'queued', 'sent', 'delivered', 'failed', 'retrying', name='delivery_status', create_type=False
)


def upgrade() -> None:
    # notifications becomes the message only — delivery state moves to alert_deliveries.
    op.drop_column('notifications', 'channel')
    op.drop_column('notifications', 'delivery_status')

    # alert_deliveries: link each delivery to its notification.
    op.add_column('alert_deliveries', sa.Column('notification_id', sa.Uuid(), nullable=True))
    op.alter_column('alert_deliveries', 'notification_id', nullable=False)  # empty table -> safe
    op.create_foreign_key(
        'fk_alert_deliveries_notification_id', 'alert_deliveries', 'notifications',
        ['notification_id'], ['id'],
    )
    op.create_index(
        op.f('ix_alert_deliveries_notification_id'), 'alert_deliveries', ['notification_id'],
        unique=False,
    )

    # backoff clock for the retry worker.
    op.add_column(
        'alert_deliveries', sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=True)
    )

    # invites/transactional notifications have no flag -> flag_id nullable.
    op.alter_column('alert_deliveries', 'flag_id', existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    op.alter_column('alert_deliveries', 'flag_id', existing_type=sa.Uuid(), nullable=False)
    op.drop_column('alert_deliveries', 'next_attempt_at')
    op.drop_index(op.f('ix_alert_deliveries_notification_id'), table_name='alert_deliveries')
    op.drop_constraint(
        'fk_alert_deliveries_notification_id', 'alert_deliveries', type_='foreignkey'
    )
    op.drop_column('alert_deliveries', 'notification_id')

    # restore the message-carried delivery columns (server_default so a non-empty table is safe).
    op.add_column(
        'notifications',
        sa.Column('delivery_status', _DELIVERY_STATUS, nullable=False, server_default='queued'),
    )
    op.add_column(
        'notifications',
        sa.Column('channel', _ALERT_CHANNEL, nullable=False, server_default='in_app'),
    )
