"""Свёртка `test_orders` в блоки — то, что переживает чистку.

Сырые сделки удалять просто так нельзя, это результат эксперимента. Но и
хранить их нельзя: на полной скорости парк пишет около 490 строк в секунду,
то есть 42 миллиона строк и 13 ГБ в сутки.

Этот воркер сворачивает уже закрытые блоки в строки вида «бот, пара,
реферальный бот, десять минут» и тем самым разрешает ретеншну удалять сырьё
за этими блоками. Ретеншн сам по себе `test_orders` не трогает: он не
опережает границу свёрнутого — см. `retention.py`.

Запускается по расписанию Celery (`app/tasks.py`), разово — так:

    python -m app.workers.test_order_rollup
"""
import asyncio
import logging
import time

from datetime import datetime, timedelta, UTC

from app.config import settings
from app.crud.test_order_rollup import TestOrderRollupCrud, floor_to_bucket
from app.dependencies import resolve_crud
from app.utils import Command, CommandResult


class TestOrderRollupCommand(Command):

    async def command(
        self,
        rollup_crud: TestOrderRollupCrud = resolve_crud(TestOrderRollupCrud),
    ) -> CommandResult:
        logging.basicConfig(
            format="%(asctime)s - %(levelname)s - %(message)s",
            level=logging.INFO,
        )

        if not settings.ROLLUP_ENABLED:
            logging.info("Свёртки выключены (ROLLUP_ENABLED=false)")
            return CommandResult(success=True, data={"buckets": 0})

        bucket = timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES)
        started_at = time.monotonic()
        deadline = started_at + settings.ROLLUP_MAX_SECONDS

        start = await self.first_unbuilt_bucket(rollup_crud, bucket)

        if start is None:
            logging.info("Сворачивать нечего: сделок в базе нет")
            return CommandResult(success=True, data={"buckets": 0})

        # Блок можно закрывать, только когда в него уже ничего не долетит:
        # сделки идут в базу через очередь в Redis, с задержкой.
        sealed_before = floor_to_bucket(
            datetime.now(UTC) - timedelta(minutes=settings.ROLLUP_LAG_MINUTES),
            bucket,
        )

        chunk = self.chunk_size(bucket)
        buckets_done = 0
        rows_written = 0

        while start + bucket <= sealed_before:
            end = min(start + chunk, sealed_before)

            rows_written += await rollup_crud.build_range(
                start=start,
                end=end,
                bucket_minutes=settings.ROLLUP_BUCKET_MINUTES,
            )
            buckets_done += int((end - start) / bucket)
            start = end

            if time.monotonic() >= deadline:
                logging.info(
                    "Время прохода вышло, хвост доберём в следующий раз"
                )
                break

        behind = max(timedelta(0), sealed_before - start)

        logging.info(
            f"Свёрнуто блоков: {buckets_done}, строк свёрток: {rows_written}, "
            f"граница свёрнутого: {start.isoformat()}, "
            f"отставание: {behind.total_seconds() / 60:.0f} мин, "
            f"за {time.monotonic() - started_at:.1f} с"
        )

        await self.clear_old_rollups(rollup_crud)

        return CommandResult(
            success=True,
            data={
                "buckets": buckets_done,
                "rows": rows_written,
                "watermark": start,
            },
        )

    @staticmethod
    def chunk_size(bucket: timedelta) -> timedelta:
        """Сколько блоков сворачивать одним запросом.

        `bucket_start` и так в GROUP BY, поэтому один запрос спокойно
        считает целый час. Разбор недельного хвоста — это 168 запросов
        вместо 1008.

        Обязательно целое число блоков: иначе край запроса разрежет блок
        пополам, и его половинки уедут в разные свёртки.
        """
        minutes = bucket.total_seconds() / 60
        buckets = max(1, int(TestOrderRollupCrud.CHUNK_MINUTES // minutes))

        return bucket * buckets

    @staticmethod
    async def first_unbuilt_bucket(
        rollup_crud: TestOrderRollupCrud, bucket: timedelta
    ) -> datetime | None:
        """Откуда продолжать: сразу за последним посчитанным блоком.

        Если свёрток ещё нет — от самой старой сырой сделки.
        """
        last = await rollup_crud.last_bucket_start()

        if last is not None:
            return floor_to_bucket(last, bucket) + bucket

        oldest = await rollup_crud.oldest_raw_order_at()

        if oldest is None:
            return None

        return floor_to_bucket(oldest, bucket)

    @staticmethod
    async def clear_old_rollups(rollup_crud: TestOrderRollupCrud) -> None:
        if not settings.RETENTION_ENABLED:
            return

        cutoff = datetime.now(UTC) - timedelta(
            days=settings.RETENTION_ROLLUP_DAYS
        )
        removed = await rollup_crud.delete_older_than(cutoff)

        if removed:
            logging.info(f"Удалено устаревших свёрток: {removed}")


async def main() -> None:
    await TestOrderRollupCommand().run_async()


if __name__ == "__main__":
    print("📦 Starting TestOrderRollupCommand...")
    asyncio.run(main())
    print("✅ TestOrderRollupCommand finished.")
