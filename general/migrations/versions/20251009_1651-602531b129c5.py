"""Rename time_to_wait column and convert to seconds

Revision ID: 602531b129c5
Revises: 2a2def7837b5
Create Date: 2025-10-09 16:51:18.550759

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '602531b129c5'
down_revision = '2a2def7837b5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE test_bots SET time_to_wait_for_entry_price_to_open_order_in_minutes = time_to_wait_for_entry_price_to_open_order_in_minutes * 60"
    )

    op.alter_column(
        'test_bots',
        'time_to_wait_for_entry_price_to_open_order_in_minutes',
        new_column_name='time_to_wait_for_entry_price_to_open_order_in_seconds',
        existing_type=sa.Numeric(precision=10, scale=2),
    )


def downgrade() -> None:
    op.alter_column(
        'test_bots',
        'time_to_wait_for_entry_price_to_open_order_in_seconds',
        new_column_name='time_to_wait_for_entry_price_to_open_order_in_minutes',
        existing_type=sa.Numeric(precision=10, scale=2),
    )

    op.execute(
        "UPDATE test_bots SET time_to_wait_for_entry_price_to_open_order_in_minutes = time_to_wait_for_entry_price_to_open_order_in_minutes / 60"
    )
