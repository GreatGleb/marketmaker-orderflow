"""Rename tables

Revision ID: 945272d672b0
Revises: ec713a426c2f
Create Date: 2025-06-17 22:09:17.059126

"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "945272d672b0"
down_revision = "ec713a426c2f"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Переименование таблиц
    op.rename_table("assets_history", "asset_history")
    op.rename_table("assets_volume_volatility", "asset_volume_volatility")

    # Переименование индексов (если нужно, зависит от префиксов)
    op.execute(
        "ALTER INDEX ix_assets_history_id " "RENAME TO ix_asset_history_id"
    )
    op.execute(
        "ALTER INDEX ix_assets_history_created_at "
        "RENAME TO ix_asset_history_created_at"
    )
    op.execute(
        "ALTER INDEX ix_assets_history_asset_exchange_id "
        "RENAME TO ix_asset_history_asset_exchange_id"
    )

    op.execute(
        "ALTER INDEX ix_assets_volume_volatility_asset_exchange_id "
        "RENAME TO ix_asset_volume_volatility_asset_exchange_id"
    )
    op.execute(
        "ALTER INDEX ix_assets_volume_volatility_created_at "
        "RENAME TO ix_asset_volume_volatility_created_at"
    )

    # Обновляем комментарий поля в другой таблице
    op.alter_column(
        "watched_pair",
        "asset_exchange_id",
        existing_type=sa.INTEGER(),
        comment="Foreign key to asset_exchange_specs",
        existing_comment="Foreign key to exchange_pair_specs",
        existing_nullable=True,
    )


def downgrade() -> None:
    # Переименование таблиц обратно
    op.rename_table("asset_history", "assets_history")
    op.rename_table("asset_volume_volatility", "assets_volume_volatility")

    # Переименование индексов обратно
    op.execute(
        "ALTER INDEX ix_asset_history_id " "RENAME TO ix_assets_history_id"
    )
    op.execute(
        "ALTER INDEX ix_asset_history_created_at "
        "RENAME TO ix_assets_history_created_at"
    )
    op.execute(
        "ALTER INDEX ix_asset_history_asset_exchange_id "
        "RENAME TO ix_assets_history_asset_exchange_id"
    )

    op.execute(
        "ALTER INDEX ix_asset_volume_volatility_asset_exchange_id "
        "RENAME TO ix_assets_volume_volatility_asset_exchange_id"
    )
    op.execute(
        "ALTER INDEX ix_asset_volume_volatility_created_at "
        "RENAME TO ix_assets_volume_volatility_created_at"
    )

    # Восстанавливаем старый комментарий
    op.alter_column(
        "watched_pair",
        "asset_exchange_id",
        existing_type=sa.INTEGER(),
        comment="Foreign key to exchange_pair_specs",
        existing_comment="Foreign key to asset_exchange_specs",
        existing_nullable=True,
    )
