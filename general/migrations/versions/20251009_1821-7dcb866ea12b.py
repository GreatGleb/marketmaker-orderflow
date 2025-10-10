"""Add 2 columns to test_bots

Revision ID: 7dcb866ea12b
Revises: 602531b129c5
Create Date: 2025-10-09 18:21:02.021930

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7dcb866ea12b'
down_revision = '602531b129c5'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'test_bots',
        sa.Column(
            'copybot_v1_check_for_24h_profitability',
            sa.Boolean(),
            nullable=True,
            server_default=sa.false(),
            comment="Checks whether bots are profitable over the last 24 hours"
        )
    )
    op.add_column(
        'test_bots',
        sa.Column(
            'copybot_v1_check_for_referral_bot_profitability',
            sa.Boolean(),
            nullable=True,
            server_default=sa.false(),
            comment="Checks whether bots are profitable when working from copybots"
        )
    )


def downgrade() -> None:
    op.drop_column('test_bots', 'copybot_v1_check_for_24h_profitability')
    op.drop_column('test_bots', 'copybot_v1_check_for_referral_bot_profitability')
