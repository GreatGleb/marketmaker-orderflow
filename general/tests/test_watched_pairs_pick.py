"""Отбор watched-пар по скачкам: проверка, что мусор не попадает в список.

Отбор целиком в SQL: передние фронты считаются оконными функциями по
секундному окну, отсечки по обороту и числу фронтов стоят в `having`, вклад
одного скачка ограничен потолком. На заглушках это не проверить, поэтому
тест засевает настоящие тики в настоящую базу.

База нужна отдельная: тест чистит `asset_history` целиком, а на рабочей базе
это снесло бы всю историю цен. Поэтому адрес передаётся явно.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists watched_check' \\
        -c 'create database watched_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/watched_check \\
      -e WATCHED_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_watched_pairs_pick

Схема накатывается `alembic upgrade head` — тест её не создаёт.
"""
import asyncio
import os

from datetime import datetime, timedelta, UTC
from decimal import Decimal

from sqlalchemy import text

from app.config import settings
from app.crud.asset_history import AssetHistoryCrud
from app.db.base import DatabaseSessionManager

WINDOW = timedelta(minutes=10)
JUMP_THRESHOLD = 0.5

INSERT_TICK = """
    INSERT INTO asset_history (
        symbol, source, last_price, quote_asset_volume_24h, event_time
    ) VALUES (
        :symbol, :source, :last_price, :quote_asset_volume_24h, :event_time
    )
"""

# Пары для засева: сколько рывков, какого размера и с каким оборотом.
#
# Каждая отсечка отвечает за свою пару: без отсечки по числу фронтов в список
# попала бы ONEJUMPUSDT (её единственный «рывок» — битый тик), без отсечки по
# обороту — CHEAPUSDT, без потолка на скачок первой стала бы CAPUSDT с её
# сорока процентами на одном тике. Правильный победитель — JUMPYUSDT: пять
# настоящих рывков.
PAIRS = [
    dict(symbol="JUMPYUSDT", price="100", volume=50_000_000,
         jumps=[1.0] * 5, source="BINANCE_SPOT"),
    dict(symbol="CAPUSDT", price="50", volume=50_000_000,
         jumps=[0.6, 0.6, 40.0], source="BINANCE_SPOT"),
    dict(symbol="ONEJUMPUSDT", price="20", volume=50_000_000,
         jumps=[5.0], source="BINANCE_SPOT"),
    dict(symbol="CHEAPUSDT", price="1", volume=500_000,
         jumps=[1.0] * 5, source="BINANCE_SPOT"),
    dict(symbol="CALMUSDT", price="10", volume=50_000_000,
         jumps=[], source="BINANCE_SPOT"),
    # Рывки есть, но записаны прежним источником и оборвались пару минут
    # назад: питатель переключили на спот, и этот ряд цен в расчёт идти не
    # должен — иначе стык двух источников сам выглядит как рывок.
    dict(symbol="OLDSOURCEUSDT", price="30", volume=50_000_000,
         jumps=[2.0] * 5, source="BINANCE", stops_early=timedelta(minutes=2)),
]

WINNER = "JUMPYUSDT"
CUT_OFF = ["ONEJUMPUSDT", "CHEAPUSDT", "CALMUSDT", "OLDSOURCEUSDT"]

# Рывок должен уложиться внутрь секундного окна, а соседние окна — остаться
# ниже порога, иначе фронт не посчитается как передний. Поэтому ровный фон
# идёт раз в полсекунды, а сам рывок — двумя тиками подряд внутри одной
# секунды, далеко друг от друга по времени.
BASELINE_STEP = timedelta(milliseconds=500)
JUMP_EVERY = timedelta(seconds=30)


def build_ticks(pair, since):
    """Ровный фон плюс отдельные рывки, разнесённые на полминуты."""
    price = Decimal(pair["price"])
    rows = []
    moment = since
    jumps = list(pair["jumps"])
    next_jump = since + JUMP_EVERY
    until = since + WINDOW - pair.get("stops_early", timedelta())

    while moment < until:
        rows.append({
            "symbol": pair["symbol"], "source": pair["source"],
            "last_price": price, "quote_asset_volume_24h": pair["volume"],
            "event_time": moment,
        })

        if jumps and moment >= next_jump:
            size = Decimal(str(jumps.pop(0)))
            rows.append({
                "symbol": pair["symbol"], "source": pair["source"],
                "last_price": price * (1 + size / 100),
                "quote_asset_volume_24h": pair["volume"],
                "event_time": moment + timedelta(milliseconds=100),
            })
            next_jump = moment + JUMP_EVERY

        moment += BASELINE_STEP

    return rows


async def seed(session, now):
    await session.execute(text("TRUNCATE asset_history"))

    rows = []

    for pair in PAIRS:
        rows += build_ticks(pair, since=now - WINDOW)

    for start in range(0, len(rows), 500):
        await session.execute(text(INSERT_TICK), rows[start:start + 500])

    await session.commit()

    return len(rows)


async def run_checks(session, now):
    since = now - WINDOW
    seeded = await seed(session, now)
    print(f"  засеяно {seeded} тиков по {len(PAIRS)} парам")

    crud = AssetHistoryCrud(session)

    # Сначала — как отбор вёл себя раньше: без отсечек в списке оказывается
    # пара с одним битым тиком, и она же первая по сумме скачков.
    was = await crud.get_top_jumpy_symbols(
        since=since, jump_threshold=JUMP_THRESHOLD, min_quote_volume_24h=0,
        min_jumps=1, jump_cap_factor=10 ** 6,
    )
    was_symbols = [row.symbol for row in was]

    assert was_symbols[0] == "CAPUSDT", (
        f"без потолка первой ожидалась CAPUSDT, а не {was_symbols[0]}"
    )
    assert "ONEJUMPUSDT" in was_symbols, "битый тик и раньше не проходил"
    assert "CHEAPUSDT" in was_symbols, "неликвид и раньше не проходил"
    print(f"    без отсечек: {len(was)} пар, первая {was_symbols[0]} "
          f"(сумма {float(was[0].jumps_sum):.1f}%), "
          f"в списке и битый тик, и неликвид")

    rows = await crud.get_top_jumpy_symbols(
        since=since, jump_threshold=JUMP_THRESHOLD
    )
    symbols = [row.symbol for row in rows]

    assert symbols, "отбор не вернул ни одной пары"
    assert symbols[0] == WINNER, f"первой выбрана {symbols[0]}, а не {WINNER}"
    print(f"    стало: {len(rows)} пар, первая {symbols[0]} "
          f"(скачков {rows[0].jumps}, сумма {float(rows[0].jumps_sum):.1f}%)")

    for symbol in CUT_OFF:
        assert symbol not in symbols, f"{symbol} прошла отбор, хотя не должна"

    print(f"    отсечены: {', '.join(CUT_OFF)}")

    # CAPUSDT остаётся в списке — у неё два настоящих рывка, — но её сорок
    # процентов на одном тике больше не выносят её на первое место.
    capped = next(row for row in rows if row.symbol == "CAPUSDT")
    limit = JUMP_THRESHOLD * 6

    assert float(capped.max_jump) > 39, "рывок на 40% в данных потерялся"
    assert float(capped.jumps_sum) <= limit * capped.jumps, (
        f"потолок не сработал: сумма {float(capped.jumps_sum):.1f}% "
        f"при {capped.jumps} скачках"
    )
    print(f"    CAPUSDT: максимум {float(capped.max_jump):.1f}%, "
          f"а в сумму вошло {float(capped.jumps_sum):.1f}% "
          f"(потолок {limit}% на скачок)")


async def main():
    print("Отбор watched-пар по скачкам:")

    if not os.getenv("WATCHED_TEST_DB"):
        print(
            "  проверка пропущена: нужна отдельная база.\n"
            "  Как её поднять — в шапке файла."
        )
        return

    print(f"  на живой базе ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        await run_checks(session, now=datetime.now(UTC))

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
