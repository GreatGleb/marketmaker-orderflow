"""Наборы пар стратегий и сборка watched_pair, на настоящей базе.

Суть проверки — независимость наборов. `watched_pair` один на всех, и
пара, выпавшая из отбора одной стратегии, не должна исчезать из общего
списка, пока нужна другой: пропали котировки — встали боты.

Внешние фильтры (бюджет покупки и торгуемость) подменяются заглушками:
они ходят за снимком Binance, а проверяем мы здесь не их.

База нужна отдельная: тест чистит `watched_pair`, `strategy_pairs` и
`test_bots`.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists strategy_check' \\
        -c 'create database strategy_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/strategy_check \\
      -e STRATEGY_PAIRS_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_strategy_pairs_db

Схему накатывает `alembic upgrade head` — тест её не создаёт.

Без `STRATEGY_PAIRS_TEST_DB` проверка пропускается.
"""
import asyncio
import os

from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text

from app.config import settings
from app.crud.strategy import StrategyCrud
from app.crud.strategy_pair import StrategyPairCrud
from app.db.base import DatabaseSessionManager
from app.db.models import AssetExchangeSpec, WatchedPair
from app.scripts import seed_watched_pairs as sw

# Пары теста: первые две — у legacy, третья — у второй стратегии,
# четвёртая ничьей стратегии не нужна и держится только ботом.
SYMBOLS = ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT")
BOT_ID = 4001


async def prepare(session):
    await session.execute(
        text("TRUNCATE watched_pair, strategy_pairs, test_bots CASCADE")
    )

    for symbol in SYMBOLS:
        await session.execute(
            text(
                """
                INSERT INTO asset_exchange_specs
                    (source, contract_type, symbol, quote_asset, status,
                     created_at, updated_at)
                VALUES ('binance', 'PERPETUAL', :symbol, 'USDT', 'TRADING',
                        now(), now())
                ON CONFLICT DO NOTHING
                """
            ),
            {"symbol": symbol},
        )

    await session.commit()

    result = await session.execute(
        select(AssetExchangeSpec.symbol, AssetExchangeSpec.id).where(
            AssetExchangeSpec.symbol.in_(SYMBOLS)
        )
    )
    return dict(result.all())


async def watched_symbols(session) -> set[str]:
    result = await session.execute(
        select(AssetExchangeSpec.symbol).join(
            WatchedPair, WatchedPair.asset_exchange_id == AssetExchangeSpec.id
        )
    )
    return set(result.scalars().all())


async def check_union(session, strategy_ids):
    """Общий список — объединение наборов, дублей нет."""
    pair_crud = StrategyPairCrud(session)
    legacy_id, other_id = strategy_ids

    await sw.apply_watched_pairs(
        session, ["AAAUSDT", "BBBUSDT"], replace=True, strategy_id=legacy_id
    )
    await sw.apply_watched_pairs(
        session, ["BBBUSDT", "CCCUSDT"], replace=True, strategy_id=other_id
    )

    assert await watched_symbols(session) == {
        "AAAUSDT", "BBBUSDT", "CCCUSDT"
    }

    # Общая пара лежит в обоих наборах — иначе удаление её у одной
    # стратегии унесло бы котировки у другой.
    assert len(await pair_crud.spec_ids_for(legacy_id)) == 2
    assert len(await pair_crud.spec_ids_for(other_id)) == 2

    rows = (await session.execute(
        select(WatchedPair.asset_exchange_id)
    )).scalars().all()
    assert len(rows) == len(set(rows)), "в watched_pair задвоились пары"

    print("  watched_pair = объединение наборов, без дублей")


async def check_independence(session, strategy_ids):
    """Пересборка набора одной стратегии не трогает чужой."""
    legacy_id, other_id = strategy_ids

    await sw.apply_watched_pairs(
        session, ["AAAUSDT"], replace=True, strategy_id=legacy_id
    )

    assert await StrategyPairCrud(session).spec_ids_for(other_id), (
        "набор второй стратегии исчез при пересборке первой"
    )

    # BBBUSDT выпала из набора legacy, но нужна второй стратегии — и
    # осталась в общем списке. CCCUSDT не тронута вовсе.
    assert await watched_symbols(session) == {
        "AAAUSDT", "BBBUSDT", "CCCUSDT"
    }

    print("  пересборка набора одной стратегии не лишает пар другую")


async def check_pinned(session, strategy_ids):
    """Пара активного бота остаётся в списке, даже если ничей набор её не просит."""
    legacy_id, other_id = strategy_ids

    await session.execute(
        text(
            """
            INSERT INTO test_bots (id, symbol, balance, is_active, strategy_id,
                                   created_at, updated_at)
            VALUES (:id, :symbol, 1000, true, :strategy, now(), now())
            ON CONFLICT (id) DO UPDATE SET
                symbol = EXCLUDED.symbol, is_active = true
            """
        ),
        {"id": BOT_ID, "symbol": "DDDUSDT", "strategy": legacy_id},
    )
    await session.commit()

    await sw.sync_watched_pairs(session)

    assert "DDDUSDT" in await watched_symbols(session), (
        "пара активного бота выпала из списка — бот остался бы без цен"
    )
    assert "DDDUSDT" not in await StrategyPairCrud(session).symbols_for(
        legacy_id
    ), "пара бота не должна попадать в набор стратегии сама собой"

    print("  пара активного бота держится в списке помимо наборов")


async def check_busy_pairs(session, strategy_ids):
    """Пара открытой позиции остаётся в списке, даже выпав из наборов.

    Состояние позиций живёт в памяти процессов симулятора, поэтому они
    сами публикуют занятые пары. Без этого пересборка снимала бы
    подписку на пару, по которой позицию ещё нужно закрывать.
    """
    legacy_id, other_id = strategy_ids

    # Ни один набор про DDDUSDT не знает, бот на ней тоже не закреплён:
    # держится она только открытой позицией.
    await session.execute(text("UPDATE test_bots SET is_active = false"))
    await session.commit()

    with patch.object(
        sw, "busy_symbols", AsyncMock(return_value=["DDDUSDT"]),
    ):
        await sw.sync_watched_pairs(session)

    assert "DDDUSDT" in await watched_symbols(session), (
        "пара открытой позиции выпала из списка — закрывать позицию "
        "будет нечем"
    )

    # Без публикации занятых пар она уходит: список не держит ничего
    # лишнего дольше необходимого.
    with patch.object(sw, "busy_symbols", AsyncMock(return_value=[])):
        await sw.sync_watched_pairs(session)

    assert "DDDUSDT" not in await watched_symbols(session)

    print("  пара открытой позиции держится в списке, пока позиция жива")


async def main():
    if not os.getenv("STRATEGY_PAIRS_TEST_DB"):
        print(
            "test_strategy_pairs_db пропущен: нужна отдельная база.\n"
            "Как её поднять — в шапке файла."
        )
        return

    print(f"Наборы пар стратегий ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        spec_ids = await prepare(session)
        assert len(spec_ids) == len(SYMBOLS)

        strategy_crud = StrategyCrud(session)
        legacy_id = await strategy_crud.ensure_legacy()
        other_id = await strategy_crud.ensure("strategy_0", "Прострелы")
        await session.commit()

        strategy_ids = (legacy_id, other_id)

        # Бюджет и торгуемость проверяются своими тестами и ходят за
        # снимком Binance; здесь они пропускают всё.
        with patch.object(
            sw, "affordable_symbols",
            AsyncMock(side_effect=lambda session, symbols: list(symbols)),
        ), patch.object(
            sw, "tradable_symbols", AsyncMock(return_value=set(SYMBOLS)),
        ):
            await check_union(session, strategy_ids)
            await check_independence(session, strategy_ids)
            await check_pinned(session, strategy_ids)
            await check_busy_pairs(session, strategy_ids)

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
