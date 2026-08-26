"""Add per-symbol commission rates to asset_exchange_specs

Revision ID: a1c4f7b93e21
Revises: 7dcb866ea12b
Create Date: 2026-08-26 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1c4f7b93e21'
down_revision = '7dcb866ea12b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'asset_exchange_specs',
        sa.Column(
            'maker_commission_rate',
            sa.Numeric(),
            nullable=True,
            comment="Maker commission rate for this symbol (0.0002 = 0.02%)"
        )
    )
    op.add_column(
        'asset_exchange_specs',
        sa.Column(
            'taker_commission_rate',
            sa.Numeric(),
            nullable=True,
            comment="Taker commission rate for this symbol (0.0004 = 0.04%)"
        )
    )


def downgrade() -> None:
    op.drop_column('asset_exchange_specs', 'taker_commission_rate')
    op.drop_column('asset_exchange_specs', 'maker_commission_rate')
