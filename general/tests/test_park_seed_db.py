"""Досев парка по стратегиям, на настоящей базе.

Раньше сид начинался с `TRUNCATE test_bots RESTART IDENTITY CASCADE`:
парк можно было только пересоздать целиком, вместе со всей историей
сделок по внешнему ключу, и завести парк второй стратегии, не разрушив
первый, было нельзя.

Проверяем то, что пришло на замену: повторный запуск не плодит дублей,
замена набора деактивирует прежние конфигурации вместо удаления, а парк
чужой стратегии остаётся нетронутым.

База нужна отдельная: тест чистит `test_bots`.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists strategy_check' \\
        -c 'create database strategy_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/strategy_check \\
      -e PARK_SEED_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_park_seed_db

Схему накатывает `alembic upgrade head` — тест её не создаёт.

Без `PARK_SEED_TEST_DB` проверка пропускается.
"""
import asyncio
import os

from decimal import Decimal
from unittest.mock import AsyncMock, patch

from sqlalchemy import func, select, text

from app.config import settings
from app.constants.strategy import STRATEGY_LEGACY
from app.crud.strategy import StrategyCrud
from app.db.base import DatabaseSessionManager
from app.db.models import TestBot
from app.scripts import new_bots

OTHER_STRATEGY = "strategy_0"

# 2000 процентных плюс копиботы: 80 v1, 20 v2, 2 v3.
EXPECTED_PARK = 2000 + 80 + 20 + 2


async def counts(session, strategy_id) -> tuple[int, int]:
    """(активных, всего) у стратегии."""
    result = await session.execute(
        select(
            func.count().filter(TestBot.is_active.is_(True)),
            func.count(),
        ).where(TestBot.strategy_id == strategy_id)
    )
    return tuple(result.one())


async def check_idempotent(session, legacy_id):
    await new_bots.create_bots(strategy=STRATEGY_LEGACY)
    active, total = await counts(session, legacy_id)

    assert (active, total) == (EXPECTED_PARK, EXPECTED_PARK), (active, total)

    await new_bots.create_bots(strategy=STRATEGY_LEGACY)
    repeated = await counts(session, legacy_id)

    assert repeated == (EXPECTED_PARK, EXPECTED_PARK), (
        f"повторный досев завёл дубли: было {EXPECTED_PARK}, стало {repeated}"
    )

    print("  повторный запуск не плодит дублей")


async def check_replace(session, legacy_id):
    await new_bots.create_bots(strategy=STRATEGY_LEGACY, replace=True)
    active, total = await counts(session, legacy_id)

    assert active == EXPECTED_PARK, f"после замены активных {active}"
    assert total == EXPECTED_PARK * 2, (
        f"прежние боты исчезли вместе с историей: всего {total}"
    )

    print("  замена набора деактивирует прежних, а не удаляет")


async def check_other_strategy(session, legacy_id, other_id):
    before = await counts(session, legacy_id)

    await new_bots.create_bots(strategy=OTHER_STRATEGY)

    assert await counts(session, other_id) == (EXPECTED_PARK, EXPECTED_PARK)
    assert await counts(session, legacy_id) == before, (
        "парк чужой стратегии изменился при засеве соседней"
    )

    print("  парк одной стратегии не задевает парк другой")


async def main():
    if not os.getenv("PARK_SEED_TEST_DB"):
        print(
            "test_park_seed_db пропущен: нужна отдельная база.\n"
            "Как её поднять — в шапке файла."
        )
        return

    print(f"Досев парка по стратегиям ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        await session.execute(text("TRUNCATE test_bots CASCADE"))
        await session.commit()

        strategy_crud = StrategyCrud(session)
        legacy_id = await strategy_crud.ensure_legacy()
        other_id = await strategy_crud.ensure(OTHER_STRATEGY, "Прострелы")
        await session.commit()

    # Средний процент за тик считается по истории цен: здесь она не
    # нужна, сетку параметров проверяет test_demo_seed.
    with patch.object(
        new_bots, "get_average_percentage_for_minimum_tick",
        AsyncMock(return_value=Decimal("0.001")),
    ):
        async with dsm.get_session() as session:
            await check_idempotent(session, legacy_id)
            await check_replace(session, legacy_id)
            await check_other_strategy(session, legacy_id, other_id)

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
