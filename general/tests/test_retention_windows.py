"""Границы, до которых ретеншну разрешено удалять.

Ошибка здесь стоит дороже всего остального в подсистеме: удалённых сделок
не вернуть. Проверяем три вещи — арифметику блоков, выравнивание запросов по
блокам и то, что чистка `test_orders` не забегает вперёд свёрток.
"""
import asyncio

from datetime import datetime, timedelta, UTC
from unittest.mock import patch

from app.config import settings
from app.crud.test_order_rollup import floor_to_bucket
from app.workers.retention import RetentionCommand
from app.workers.test_order_rollup import TestOrderRollupCommand


class FakeRollupCrud:
    """Свёртки посчитаны до `last` (это начало последнего блока)."""

    def __init__(self, last):
        self.last = last

    async def last_bucket_start(self):
        return self.last


def check_bucket_math():
    bucket = timedelta(minutes=10)

    cases = {
        "2026-09-07T12:00:00+00:00": "2026-09-07T12:00:00+00:00",
        "2026-09-07T12:09:59+00:00": "2026-09-07T12:00:00+00:00",
        "2026-09-07T12:10:00+00:00": "2026-09-07T12:10:00+00:00",
        "2026-09-07T23:59:59+00:00": "2026-09-07T23:50:00+00:00",
    }

    for moment, expected in cases.items():
        got = floor_to_bucket(datetime.fromisoformat(moment), bucket)
        assert got.isoformat() == expected, f"{moment} → {got.isoformat()}"

    print("  начало блока считается от эпохи, как date_bin в базе — сходится")


def check_chunk_alignment():
    """Запрос обязан покрывать целое число блоков.

    Иначе край запроса разрежет блок пополам, половинки попадут в разные
    вызовы, и вторая перезапишет первую по уникальному ключу — часть сделок
    просто исчезнет из статистики.
    """
    for minutes in (1, 5, 10, 15, 30, 60, 90):
        bucket = timedelta(minutes=minutes)
        chunk = TestOrderRollupCommand.chunk_size(bucket)

        assert chunk >= bucket, f"чанк меньше блока при {minutes} мин"
        assert chunk % bucket == timedelta(0), (
            f"чанк {chunk} не кратен блоку {bucket}"
        )

    print("  чанк всегда кратен блоку — блок не разрежется пополам")


async def check_cutoff_never_outruns_rollups():
    now = datetime.fromisoformat("2026-09-07T12:00:00+00:00")
    cutoff = now - timedelta(hours=settings.RETENTION_TEST_ORDERS_HOURS)

    # Свёртки идут вровень: чистим по окну хранения.
    crud = FakeRollupCrud(now - timedelta(hours=2))
    got = await RetentionCommand.test_orders_cutoff(crud, now)
    assert got == cutoff, got

    # Свёртки отстали на неделю: дальше их границы не лезем, даже если по
    # окну хранения давно пора.
    behind = now - timedelta(days=7)
    crud = FakeRollupCrud(behind)
    got = await RetentionCommand.test_orders_cutoff(crud, now)
    expected = behind + timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES)
    assert got == expected, got
    assert got < cutoff, "отставшие свёртки обязаны сдвигать границу назад"

    # Свёрток нет вовсе (первый запуск): сырое не трогаем ни строки.
    got = await RetentionCommand.test_orders_cutoff(FakeRollupCrud(None), now)
    assert got is None, got

    print("  чистка test_orders не забегает вперёд свёрток")


async def check_window_covers_longest_copybot_window():
    """48 часов — не круглое число, а самое длинное окно копиботов.

    `new_bots.py` раздаёт копиботам окна вплоть до 2880 минут. Если хранить
    сырые сделки меньше, длинные окна начнут считаться по обрезанной истории
    и рейтинг поедет — молча, без единой ошибки в логе.
    """
    longest_window_minutes = 2880

    kept_minutes = settings.RETENTION_TEST_ORDERS_HOURS * 60

    assert kept_minutes >= longest_window_minutes, (
        f"RETENTION_TEST_ORDERS_HOURS="
        f"{settings.RETENTION_TEST_ORDERS_HOURS} меньше самого длинного окна "
        f"копиботов ({longest_window_minutes} мин)"
    )

    print(
        f"  окно хранения {settings.RETENTION_TEST_ORDERS_HOURS:.0f} ч "
        f"покрывает самое длинное окно копиботов "
        f"({longest_window_minutes / 60:.0f} ч)"
    )


async def check_disabled_rollups_are_loud():
    """Без свёрток сырьё удаляется безвозвратно — это должно быть видно."""
    now = datetime.now(UTC)

    with patch.object(settings, "ROLLUP_ENABLED", False):
        with patch("app.workers.retention.logging") as log:
            got = await RetentionCommand.test_orders_cutoff(
                FakeRollupCrud(None), now
            )

    assert got is not None, "без свёрток чистка идёт по окну хранения"
    assert log.warning.called, "потеря статистики обязана попадать в лог"

    print("  выключенные свёртки предупреждают о потере статистики")


async def main():
    print("Границы ретеншна:")
    check_bucket_math()
    check_chunk_alignment()
    await check_cutoff_never_outruns_rollups()
    await check_window_covers_longest_copybot_window()
    await check_disabled_rollups_are_loud()
    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
