"""Rename table

Revision ID: ec713a426c2f
Revises: 5d5fc60bcb99
Create Date: 2025-06-17 17:06:53.982419

"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "ec713a426c2f"
down_revision = "5d5fc60bcb99"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Переименование таблицы
    op.rename_table("exchange_pair_specs", "asset_exchange_specs")

    # Переименование индексов
    op.execute(
        "ALTER INDEX ix_exchange_pair_specs_asset_pairs_id "
        "RENAME TO ix_asset_exchange_specs_asset_pairs_id"
    )
    op.execute(
        "ALTER INDEX ix_exchange_pair_specs_created_at "
        "RENAME TO ix_asset_exchange_specs_created_at"
    )

    # Переименование внешнего ключа в assets_history
    op.alter_column(
        "assets_history",
        "exchange_pair_id",
        new_column_name="asset_exchange_id",
    )
    op.drop_index(
        "ix_assets_history_exchange_pair_id", table_name="assets_history"
    )
    op.create_index(
        "ix_assets_history_asset_exchange_id",
        "assets_history",
        ["asset_exchange_id"],
        unique=False,
    )
    op.drop_constraint(
        "assets_history_exchange_pair_id_fkey",
        "assets_history",
        type_="foreignkey",
    )
    op.create_foreign_key(
        None,
        "assets_history",
        "asset_exchange_specs",
        ["asset_exchange_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # В assets_volume_volatility
    op.alter_column(
        "assets_volume_volatility",
        "exchange_pair_id",
        new_column_name="asset_exchange_id",
    )
    op.drop_index(
        "ix_assets_volume_volatility_exchange_pair_id",
        table_name="assets_volume_volatility",
    )
    op.create_index(
        "ix_assets_volume_volatility_asset_exchange_id",
        "assets_volume_volatility",
        ["asset_exchange_id"],
        unique=False,
    )
    op.drop_constraint(
        "assets_volume_volatility_exchange_pair_id_fkey",
        "assets_volume_volatility",
        type_="foreignkey",
    )
    op.create_foreign_key(
        None,
        "assets_volume_volatility",
        "asset_exchange_specs",
        ["asset_exchange_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # В watched_pair
    op.alter_column(
        "watched_pair", "exchange_pair_id", new_column_name="asset_exchange_id"
    )
    op.drop_index(
        "ix_watched_pair_exchange_pair_id", table_name="watched_pair"
    )
    op.create_index(
        "ix_watched_pair_asset_exchange_id",
        "watched_pair",
        ["asset_exchange_id"],
        unique=False,
    )
    op.drop_constraint(
        "watched_pair_exchange_pair_id_fkey",
        "watched_pair",
        type_="foreignkey",
    )
    op.create_foreign_key(
        None,
        "watched_pair",
        "asset_exchange_specs",
        ["asset_exchange_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # Обратно в watched_pair
    op.drop_constraint(None, "watched_pair", type_="foreignkey")
    op.alter_column(
        "watched_pair", "asset_exchange_id", new_column_name="exchange_pair_id"
    )
    op.drop_index(
        "ix_watched_pair_asset_exchange_id", table_name="watched_pair"
    )
    op.create_index(
        "ix_watched_pair_exchange_pair_id",
        "watched_pair",
        ["exchange_pair_id"],
        unique=False,
    )
    op.create_foreign_key(
        "watched_pair_exchange_pair_id_fkey",
        "watched_pair",
        "exchange_pair_specs",
        ["exchange_pair_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Обратно в assets_volume_volatility
    op.drop_constraint(None, "assets_volume_volatility", type_="foreignkey")
    op.alter_column(
        "assets_volume_volatility",
        "asset_exchange_id",
        new_column_name="exchange_pair_id",
    )
    op.drop_index(
        "ix_assets_volume_volatility_asset_exchange_id",
        table_name="assets_volume_volatility",
    )
    op.create_index(
        "ix_assets_volume_volatility_exchange_pair_id",
        "assets_volume_volatility",
        ["exchange_pair_id"],
        unique=False,
    )
    op.create_foreign_key(
        "assets_volume_volatility_exchange_pair_id_fkey",
        "assets_volume_volatility",
        "exchange_pair_specs",
        ["exchange_pair_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Обратно в assets_history
    op.drop_constraint(None, "assets_history", type_="foreignkey")
    op.alter_column(
        "assets_history",
        "asset_exchange_id",
        new_column_name="exchange_pair_id",
    )
    op.drop_index(
        "ix_assets_history_asset_exchange_id", table_name="assets_history"
    )
    op.create_index(
        "ix_assets_history_exchange_pair_id",
        "assets_history",
        ["exchange_pair_id"],
        unique=False,
    )
    op.create_foreign_key(
        "assets_history_exchange_pair_id_fkey",
        "assets_history",
        "exchange_pair_specs",
        ["exchange_pair_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Переименование индексов обратно
    op.execute(
        "ALTER INDEX ix_asset_exchange_specs_asset_pairs_id "
        "RENAME TO ix_exchange_pair_specs_asset_pairs_id"
    )
    op.execute(
        "ALTER INDEX ix_asset_exchange_specs_created_at "
        "RENAME TO ix_exchange_pair_specs_created_at"
    )

    # Переименование таблицы обратно
    op.rename_table("asset_exchange_specs", "exchange_pair_specs")
