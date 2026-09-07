"""Чистка растущих таблиц — единственное, что не даёт диску кончиться.

Считать нечего: `asset_history` пишет каждый тик по каждой наблюдаемой паре,
`test_orders` — каждую закрытую сделку всех ботов парка. Вместе это гигабайты
в сутки, а диск на сервере вроде CCX13 — 80 ГБ. Без этого прохода он
кончается примерно за неделю.

Что делает проход:

* `asset_history` и `asset_order_book` — удаляет всё старше своего окна.
  Обе таблицы дальше суток никем не читаются;
* `test_orders` — удаляет только то, что уже свёрнуто в `test_order_rollups`.
  Сырые сделки это результат эксперимента, и терять его нельзя: удаляется
  только то, от чего осталась статистика.

Удаление идёт пачками с коммитом после каждой и с потолком по времени: один
`DELETE` на десятки миллионов строк раздувает WAL и держит базу, а не успел
за проход — доберёт на следующем часе.

Место операционной системе обычный `DELETE` не возвращает, он возвращает его
самой таблице: та перестаёт расти, потому что пишет в освобождённые страницы.
Для сервера это ровно то, что нужно. Если место нужно вернуть именно системе
(например, разово после долгого простоя чистки) — `RETENTION_REPACK=true`,
но помните, что pg_repack требует свободного места в размер таблицы вместе с
индексами, и на заполненном диске он его добьёт.

Запускается по расписанию Celery (`app/tasks.py`), разово — так:

    python -m app.workers.retention
"""
import asyncio
import logging
import time

from datetime import datetime, timedelta, UTC

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from app.config import settings
from app.crud.asset_history import AssetHistoryCrud
from app.crud.asset_order_book import AssetOrderBookCrud
from app.crud.test_order_rollup import TestOrderRollupCrud
from app.crud.test_orders import TestOrderCrud
from app.dependencies import get_session, resolve_crud
from app.utils import Command, CommandResult
from app.workers.repack import repack_table


class RetentionCommand(Command):

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        asset_crud: AssetHistoryCrud = resolve_crud(AssetHistoryCrud),
        order_book_crud: AssetOrderBookCrud = resolve_crud(
            AssetOrderBookCrud
        ),
        order_crud: TestOrderCrud = resolve_crud(TestOrderCrud),
        rollup_crud: TestOrderRollupCrud = resolve_crud(TestOrderRollupCrud),
    ) -> CommandResult:
        logging.basicConfig(
            format="%(asctime)s - %(levelname)s - %(message)s",
            level=logging.INFO,
        )

        if not settings.RETENTION_ENABLED:
            logging.info("Чистка выключена (RETENTION_ENABLED=false)")
            return CommandResult(
                success=True, data={"deleted": {}, "finished": {}}
            )

        started_at = time.monotonic()
        now = datetime.now(UTC)

        # У каждой таблицы своя контрольная точка по времени, треть потолка
        # на каждую. Точки абсолютные и накопительные: управился первый
        # быстрее своей трети — остаток достаётся следующим, а не сгорает.
        #
        # Порядок — по тому, кто съедает больше диска. Если проход всё-таки
        # упрётся в потолок, недобранным окажется самое мелкое, а не самое
        # крупное. `test_orders` при полном парке даёт 13 ГБ в сутки против
        # 473 МБ у `asset_history`, поэтому идёт первым.
        share = settings.RETENTION_MAX_SECONDS / 3

        deleted = {}
        finished = {}

        # Граница для сделок считается отдельно: она упирается не только в
        # срок хранения, но и в то, докуда посчитаны свёртки.
        orders_cutoff = await self.test_orders_cutoff(rollup_crud, now)

        tables = [
            ("test_orders", order_crud, "created_at", orders_cutoff),
            (
                "asset_history",
                asset_crud,
                "event_time",
                now - timedelta(hours=settings.RETENTION_ASSET_HISTORY_HOURS),
            ),
            (
                # Не `transaction_time`: он не проиндексирован, и чистка по
                # нему читала бы таблицу целиком. `created_at` — время
                # вставки, при хранении в сутки это то же самое с точностью
                # до секунд.
                "asset_order_book",
                order_book_crud,
                "created_at",
                now - timedelta(hours=settings.RETENTION_ORDER_BOOK_HOURS),
            ),
        ]

        for number, (table, crud, column, cutoff) in enumerate(tables, 1):
            if cutoff is None:
                deleted[table], finished[table] = 0, True
                continue

            deleted[table], finished[table] = await self.sweep(
                crud=crud,
                table=table,
                column=column,
                cutoff=cutoff,
                deadline=started_at + number * share,
            )

        await self.log_sizes(session)

        if settings.RETENTION_REPACK and all(finished.values()):
            for table in ("asset_history", "test_orders"):
                await repack_table(table)

        logging.info(
            f"Чистка закончена за {time.monotonic() - started_at:.1f} с, "
            f"удалено строк: {sum(deleted.values())}"
        )

        return CommandResult(
            success=True, data={"deleted": deleted, "finished": finished}
        )

    @staticmethod
    async def sweep(crud, table: str, column: str, cutoff, deadline):
        """Чистит одну таблицу и рассказывает, чем дело кончилось."""
        started_at = time.monotonic()

        rows, complete = await crud.delete_older_than_in_batches(
            column_name=column,
            cutoff=cutoff,
            batch_rows=settings.RETENTION_BATCH_ROWS,
            deadline=deadline,
        )

        tail = "" if complete else ", хвост остался на следующий проход"
        logging.info(
            f"{table}: удалено {rows} строк старше "
            f"{cutoff.isoformat()} за {time.monotonic() - started_at:.1f} с"
            f"{tail}"
        )

        return rows, complete

    @staticmethod
    async def test_orders_cutoff(
        rollup_crud: TestOrderRollupCrud, now: datetime
    ) -> datetime | None:
        """До какого момента сырые сделки разрешено удалять.

        Два ограничения, берём более раннее:

        * окно хранения. Меньше 48 часов оставлять нельзя: самое длинное окно
          прибыльности копиботов — 2880 минут, и на обрезанной истории оно
          начнёт считаться по неполным данным;
        * граница свёрнутого. Всё, что ещё не попало в `test_order_rollups`,
          не удаляется вовсе — иначе от этих сделок не останется ничего.

        `None` означает «удалять нечего»: свёрток пока нет.
        """
        cutoff = now - timedelta(hours=settings.RETENTION_TEST_ORDERS_HOURS)

        if not settings.ROLLUP_ENABLED:
            logging.warning(
                "Свёртки выключены: сырые сделки старше "
                f"{settings.RETENTION_TEST_ORDERS_HOURS} ч удаляются "
                "безвозвратно, статистика по ним не сохранится"
            )
            return cutoff

        last_bucket = await rollup_crud.last_bucket_start()

        if last_bucket is None:
            logging.info(
                "test_orders: свёрток ещё нет, сырые сделки не трогаем"
            )
            return None

        # Свёрнут сам блок, значит безопасная граница — его конец.
        watermark = last_bucket + timedelta(
            minutes=settings.ROLLUP_BUCKET_MINUTES
        )

        if watermark < cutoff:
            logging.info(
                "test_orders: свёртки отстают "
                f"({watermark.isoformat()} против {cutoff.isoformat()}), "
                "чистим только до границы свёрнутого"
            )
            return watermark

        return cutoff

    @staticmethod
    async def log_sizes(session: AsyncSession) -> None:
        """Размеры таблиц после прохода — это то, ради чего всё затевалось."""
        result = await session.execute(
            text(
                """
                SELECT relname,
                       pg_size_pretty(pg_total_relation_size(relid)) AS size
                FROM pg_catalog.pg_statio_user_tables
                WHERE relname IN (
                    'asset_history', 'asset_order_book',
                    'test_orders', 'test_order_rollups'
                )
                ORDER BY pg_total_relation_size(relid) DESC
                """
            )
        )

        sizes = ", ".join(f"{name} {size}" for name, size in result.all())
        logging.info(f"Размеры таблиц: {sizes}")


async def main() -> None:
    await RetentionCommand().run_async()


if __name__ == "__main__":
    print("🧹 Starting RetentionCommand...")
    asyncio.run(main())
    print("✅ RetentionCommand finished.")
