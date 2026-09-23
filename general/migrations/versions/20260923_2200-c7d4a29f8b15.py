"""Лучшие цены книги в asset_history: спред перестаёт быть невидимым

Revision ID: c7d4a29f8b15
Revises: b3f81a6c4d29
Create Date: 2026-09-23 22:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c7d4a29f8b15'
down_revision = 'b3f81a6c4d29'
branch_labels = None
depends_on = None


# Колонки пустые у тех источников, которые bid/ask не отдают вовсе
# (фьючерсный !ticker@arr, REST /fapi/v1/ticker/24hr). Пересчитывать
# накопленные строки нечем: середина спреда необратима.
COLUMNS = (
    ('best_bid_price', 'Best bid price (b)'),
    ('best_bid_qty', 'Best bid quantity (B)'),
    ('best_ask_price', 'Best ask price (a)'),
    ('best_ask_qty', 'Best ask quantity (A)'),
)


def upgrade() -> None:
    for name, comment in COLUMNS:
        op.add_column(
            'asset_history',
            sa.Column(name, sa.Numeric(), nullable=True, comment=comment),
        )


def downgrade() -> None:
    for name, _ in reversed(COLUMNS):
        op.drop_column('asset_history', name)
