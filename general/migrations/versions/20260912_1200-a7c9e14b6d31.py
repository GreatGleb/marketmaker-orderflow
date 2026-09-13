"""Копибот v3: третий уровень копирования

Боевой `binance_bot` уже работает как копибот третьего уровня: в
`_get_best_copy_bot` он берёт лучшего копибота v2 за зашитые в код 12 часов,
по окну этого v2 — лучшего копибота v1, а по окну и фильтрам того — обычного
бота, чьим конфигом и торгует. Отдельной строки в `test_bots` под это не было,
поэтому и предсказать результат боевого бота было нечем: в парке отсутствовал
бот, проходящий ту же цепочку.

Колонки заводят такого бота в парке.

`copybot_v3_time_in_minutes` — признак типа и окно, за которое оцениваются
копиботы v2. Тип бота здесь нигде не хранится отдельным полем, он выводится из
того, какая колонка-маркер не NULL; v3 встаёт в этот ряд третьим. Значение 720
повторяет 12 часов из боевого бота.

`copybot_v3_compound_balance` различает два варианта, которые заводятся парой.
Без флага баланс всегда 1000, как у всего парка, и бот сравним с остальными в
общих отчётах. С флагом баланс меняется от сделки к сделке, в позицию идёт 99%
счёта и количество округляется по шагу лота — это и есть прогноз в деньгах.
Разница между вариантами не сводится к множителю: PnL линеен по балансу, а вот
минимальный лот — нет, и именно он показывает, где реальный бот перестанет
торговать.

`copybot_v3_stopped_at` — отметка времени, когда на балансе перестал набираться
минимальный лот. Остановка намеренно не делается через `is_active = false`:
подзапрос `active_bots_subquery` отбирает только активных, и снятие флага
убрало бы бота из всех отчётов — ровно там, где факт остановки важнее всего.

Revision ID: a7c9e14b6d31
Revises: f3a8b0d47c19
Create Date: 2026-09-12 12:00:00.000000

"""
import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "a7c9e14b6d31"
down_revision = "f3a8b0d47c19"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "test_bots",
        sa.Column(
            "copybot_v3_time_in_minutes",
            sa.Numeric(),
            nullable=True,
            comment=(
                "For copy bot version 3 the time window over which copy bots "
                "v2 are ranked. Not null marks the bot as a copybot v3"
            ),
        ),
    )
    op.add_column(
        "test_bots",
        sa.Column(
            "copybot_v3_compound_balance",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment=(
                "Reinvest profit and trade 99% of the running balance with "
                "lot size rounding, mirroring the live bot. False keeps the "
                "fixed 1000 balance used by the rest of the park"
            ),
        ),
    )
    op.add_column(
        "test_bots",
        sa.Column(
            "copybot_v3_stopped_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the balance stopped covering the minimum lot. The bot "
                "stays active so that it keeps showing up in reports"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("test_bots", "copybot_v3_stopped_at")
    op.drop_column("test_bots", "copybot_v3_compound_balance")
    op.drop_column("test_bots", "copybot_v3_time_in_minutes")
