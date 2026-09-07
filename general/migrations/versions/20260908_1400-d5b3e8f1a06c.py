"""Убрать дубль индекса по asset_history.id

`id` объявлялся как `mapped_column(..., primary_key=True, index=True)`.
Первичный ключ и так создаёт уникальный btree (`assets_history_pkey`), а
`index=True` добавляет второй, ровно по той же колонке
(`ix_asset_history_id`). Один из двух не нужен никому.

Пользы от него нет ни в одном сценарии. По `asset_history.id` во всём коде
ходит единственное место — `app/scripts/new_bots.py`, где последняя цена по
паре достаётся джойном на `id` ранжирующего CTE. План этого запроса и план
точечного `where id = ?` не меняются вообще: планировщик просто берёт
`assets_history_pkey` с той же стоимостью (замер на 1.58 млн строк — узел
`Index Scan`, cost 0.43..6.51, до и после совпадает до сотых). Уникальный
индекс по одной колонке перекрывает обычный по ней же полностью.

Стоит он при этом как полноценный индекс: на 2 млн строк 43 МБ — столько же,
сколько сам PK, и около 11% всех индексов таблицы. Плюс каждая вставка
обновляет его наравне с остальными, а autovacuum обходит целиком на каждом
проходе; у `asset_history` проход и без того самый дорогой в базе.

Остальные пять оставлены: `idx_assethistory_symbol_event_price` считает
волатильность, `ix_asset_history_event_time` — чистка по времени,
`ix_asset_history_created_at` сортирует в `new_bots.py`,
`ix_asset_history_asset_exchange_id` держит внешний ключ, `assets_history_pkey`
— собственно ключ.

Удаляется через `CONCURRENTLY`: обычный `DROP INDEX` берёт
`ACCESS EXCLUSIVE` на таблицу, а в неё непрерывно пишет питатель цен и
секундами читают запросы волатильности — ждать их под блокировкой значит
остановить вставку. Поэтому шаг вынесен из транзакции миграции.

Комментарий с колонки снимается заодно: он приехал из того же объявления,
которое теперь целиком заменено наследованием от `BigId`, и у остальных
потоковых таблиц (`test_orders`, `asset_order_book`) `id` без комментария.

Revision ID: d5b3e8f1a06c
Revises: c4a91e7b2d38
Create Date: 2026-09-08 14:00:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = 'd5b3e8f1a06c'
down_revision = 'c4a91e7b2d38'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("COMMENT ON COLUMN asset_history.id IS NULL")

    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_asset_history_id")


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_asset_history_id "
            "ON asset_history (id)"
        )

    op.execute("COMMENT ON COLUMN asset_history.id IS 'Primary key'")
