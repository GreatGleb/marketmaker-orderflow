"""Rename field

Revision ID: dfc4c6fcf213
Revises: 945272d672b0
Create Date: 2025-06-17 22:42:30.543788

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "dfc4c6fcf213"
down_revision = "945272d672b0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename columns in asset_history
    op.alter_column(
        "asset_history",
        "high_24h",
        new_column_name="price_high_24h",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="24h high price (h)",
    )
    op.alter_column(
        "asset_history",
        "low_24h",
        new_column_name="price_low_24h",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="24h low price (l)",
    )

    # Adjust asset_exchange_id comment
    op.alter_column(
        "asset_history",
        "asset_exchange_id",
        existing_type=sa.INTEGER(),
        existing_nullable=True,
        existing_comment="Foreign key to exchange_pair_specs",
        comment="Foreign key to asset_exchange_specs",
    )

    # Rename columns in asset_volume_volatility
    op.alter_column(
        "asset_volume_volatility",
        "price_high",
        new_column_name="price_high_24h",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="High 24h price",
    )
    op.alter_column(
        "asset_volume_volatility",
        "price_low",
        new_column_name="price_low_24h",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="Low 24h price",
    )

    # Adjust asset_exchange_id comment
    op.alter_column(
        "asset_volume_volatility",
        "asset_exchange_id",
        existing_type=sa.INTEGER(),
        existing_nullable=True,
        existing_comment="Foreign key to exchange_pair_specs",
        comment="Foreign key to asset_exchange_specs",
    )


def downgrade() -> None:
    # Revert column names in asset_volume_volatility
    op.alter_column(
        "asset_volume_volatility",
        "price_high_24h",
        new_column_name="price_high",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="High 24h price",
    )
    op.alter_column(
        "asset_volume_volatility",
        "price_low_24h",
        new_column_name="price_low",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="Low 24h price",
    )

    op.alter_column(
        "asset_volume_volatility",
        "asset_exchange_id",
        existing_type=sa.INTEGER(),
        existing_nullable=True,
        existing_comment="Foreign key to asset_exchange_specs",
        comment="Foreign key to exchange_pair_specs",
    )

    # Revert column names in asset_history
    op.alter_column(
        "asset_history",
        "price_high_24h",
        new_column_name="high_24h",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="24h high price (h)",
    )
    op.alter_column(
        "asset_history",
        "price_low_24h",
        new_column_name="low_24h",
        existing_type=sa.Numeric(),
        existing_nullable=True,
        comment="24h low price (l)",
    )

    op.alter_column(
        "asset_history",
        "asset_exchange_id",
        existing_type=sa.INTEGER(),
        existing_nullable=True,
        existing_comment="Foreign key to asset_exchange_specs",
        comment="Foreign key to exchange_pair_specs",
    )
