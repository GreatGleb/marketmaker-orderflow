"""Пул доноров копиботов — любая стратегия

Копиботы заводились с явным пулом `{"mode": "list", "strategies":
["legacy"]}`. Причина была: пока стратегия одна, пул `all` означал бы,
что копибот сменит алгоритм сам собой в день подключения второй, и
сравнивать его результаты до и после станет нельзя.

Второй и третья стратегии подключены (`strategy_0`, `strategy_1`), и
теперь тот же список работает наоборот: копиботы остаются запертыми на
`legacy` и о новых парках не узнают никогда. Копибот — уровень
копирования, а не торговая стратегия: он обязан уметь наследовать
конфиг любого бота, а каким алгоритмом торговать, ему говорит стратегия
донора (`executed_strategy_id` в сделке).

Сопоставимость это рвёт, и разделять надо не по дате миграции, а по
`test_orders.executed_strategy_id`: он для того и заведён. Сделки
копиботов до этого дня все помечены `legacy`, после — стратегией того
донора, чей конфиг исполнялся.

Трогаются только копиботы с ровно этим пулом. Пул, суженный руками до
другого набора, — осознанное решение, и переписывать его миграцией
нельзя.

Боевого `binance_bot` это не касается: он умеет один алгоритм и берёт
донора не через пул копибота, а своим запросом — там пул `legacy`
проставлен в коде.

Revision ID: f4a17c98b2e0
Revises: e6c94a17f0d3
Create Date: 2026-09-20 12:00:00.000000

"""
import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "f4a17c98b2e0"
down_revision = "e6c94a17f0d3"
branch_labels = None
depends_on = None


LEGACY_ONLY = '{"mode": "list", "strategies": ["legacy"]}'
ALL_STRATEGIES = '{"mode": "all"}'


def _move(connection, source: str, target: str) -> int:
    result = connection.execute(
        sa.text(
            """
            UPDATE test_bots
            SET donor_scope = CAST(:target AS jsonb)
            WHERE bot_kind IN ('copy_v1', 'copy_v2', 'copy_v3')
              AND donor_scope = CAST(:source AS jsonb)
            """
        ),
        {"source": source, "target": target},
    )

    return result.rowcount


def upgrade() -> None:
    connection = op.get_bind()
    moved = _move(connection, LEGACY_ONLY, ALL_STRATEGIES)

    print(
        f"✅ Копиботов с пулом «любая стратегия»: {moved}. Их сделки теперь "
        f"различаются по test_orders.executed_strategy_id, а не по дате."
    )

    other = connection.execute(
        sa.text(
            """
            SELECT count(*) FROM test_bots
            WHERE bot_kind IN ('copy_v1', 'copy_v2', 'copy_v3')
              AND donor_scope IS NOT NULL
              AND donor_scope <> CAST(:target AS jsonb)
            """
        ),
        {"target": ALL_STRATEGIES},
    ).scalar_one()

    if other:
        print(
            f"ℹ️  Копиботов с другим пулом: {other}. Не тронуты: сужение "
            f"пула руками — осознанное решение."
        )


def downgrade() -> None:
    connection = op.get_bind()
    moved = _move(connection, ALL_STRATEGIES, LEGACY_ONLY)

    print(f"Копиботов возвращено к пулу только из legacy: {moved}.")
