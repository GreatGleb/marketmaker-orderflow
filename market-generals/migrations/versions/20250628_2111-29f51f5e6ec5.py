"""Update bot model

Revision ID: 29f51f5e6ec5
Revises: 3f1aaa014298
Create Date: 2025-06-28 21:11:23.950867

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "29f51f5e6ec5"
down_revision = "3f1aaa014298"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Переименование колонки exit_offset_ticks -> take_profit_ticks
    op.alter_column(
        "test_bots",
        "exit_offset_ticks",
        new_column_name="take_profit_ticks",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_comment="Target Profit/Close in Ticks",
        comment="Target Profit/Close in Ticks",
    )

    # Добавляем колонку successful_stop_lose_ticks
    op.add_column(
        "test_bots",
        sa.Column(
            "successful_stop_lose_ticks",
            sa.Integer(),
            nullable=False,
            server_default="10",
            comment="Stop-loss in ticks after trade "
            "becomes profitable (used to secure partial profit)",
        ),
    )


def downgrade() -> None:
    # Удаляем колонку successful_stop_lose_ticks
    op.drop_column("test_bots", "successful_stop_lose_ticks")

    # Переименование колонки обратно: take_profit_ticks -> exit_offset_ticks
    op.alter_column(
        "test_bots",
        "take_profit_ticks",
        new_column_name="exit_offset_ticks",
        existing_type=sa.Integer(),
        existing_nullable=False,
        existing_comment="Target Profit/Close in Ticks",
        comment="Target Profit/Close in Ticks",
    )
