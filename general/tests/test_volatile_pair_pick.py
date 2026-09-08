"""Отбор волатильной пары: проверка, что мусор не выигрывает.

Вся суть отбора — в SQL: отсечки по обороту и числу тиков стоят в `having`, а
одиночные выбросы отбрасываются вместе с парой самых крайних отпечатков. На
заглушках это не проверить, поэтому тест засевает настоящие тики в настоящую
базу.

Отдельно проверяется, что потолка на реальное движение нет: пара, сходившая
на 25%, обязана выиграть у пары, стоящей на месте с одним битым тиком +50%.

База нужна отдельная: тест чистит `asset_history` целиком, а на рабочей базе
это снесло бы всю историю цен. Поэтому адрес передаётся явно.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists volatile_check' \\
        -c 'create database volatile_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/volatile_check \\
      -e VOLATILE_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_volatile_pair_pick

Схема накатывается `alembic upgrade head` — тест её не создаёт.

Без `VOLATILE_TEST_DB` проверка пропускается: остальные тесты в каталоге
идут на заглушках и не должны требовать поднятого стека.
"""
import asyncio
import os

from datetime import datetime, timedelta, UTC
from decimal import Decimal

from sqlalchemy import text

from app.config import settings
from app.constants.volatility import MIN_TICKS_IN_WINDOW
from app.crud.asset_history import AssetHistoryCrud
from app.db.base import DatabaseSessionManager

WINDOW = timedelta(minutes=5)

# Пары для засева: форма движения, цена, размах в процентах, число тиков,
# оборот за сутки и битые тики, если они у пары есть.
#
# Расклад подобран так, чтобы каждая отсечка отвечала за свою пару: без
# отсечки по обороту победила бы THINUSDT, без отсечки по тикам —
# SLEEPYUSDT, без отбрасывания крайних тиков — GLITCHUSDT или JUNKUSDT.
# Правильный ответ — STRONGUSDT: единственная, у которой большое движение
# настоящее, то есть подтверждённое сотнями тиков.
PAIRS = [
    # настоящий ход на 25% — тиков по всему диапазону, войти было можно
    dict(symbol="STRONGUSDT", shape="ramp", price="100", spread_pct="25",
         ticks=300, volume=50_000_000, broken=[]),
    # ликвидная, движение на 2% — нормальный кандидат, но слабее STRONG
    dict(symbol="REALUSDT", shape="saw", price="100", spread_pct="2",
         ticks=300, volume=50_000_000, broken=[]),
    # стоит на месте, зато с одним битым тиком +50%
    dict(symbol="GLITCHUSDT", shape="saw", price="10", spread_pct="0.1",
         ticks=300, volume=50_000_000, broken=["15"]),
    # два битых тика с одной стороны — ровно на это и рассчитан запас в две
    # штуки, одним отброшенным тиком тут было бы не обойтись
    dict(symbol="TWOGLITCHUSDT", shape="saw", price="10", spread_pct="0.1",
         ticks=300, volume=50_000_000, broken=["15", "16"]),
    # размах 10%, но оборот копеечный: торговать таким нечем
    dict(symbol="THINUSDT", shape="saw", price="5", spread_pct="10",
         ticks=300, volume=500_000, broken=[]),
    # оборот большой, но за окно всего пять тиков: считать не по чему
    dict(symbol="SLEEPYUSDT", shape="saw", price="20", spread_pct="5",
         ticks=5, volume=50_000_000, broken=[]),
    # неликвид с битым тиком втрое — победитель прежнего отбора
    dict(symbol="JUNKUSDT", shape="saw", price="1", spread_pct="0.1",
         ticks=6, volume=300_000, broken=["3"]),
]

WINNER = "STRONGUSDT"
CUT_OFF = ["THINUSDT", "SLEEPYUSDT", "JUNKUSDT"]
FLAT_WITH_GLITCH = ["GLITCHUSDT", "TWOGLITCHUSDT"]


def build_ticks(pair, since, until):
    """Тики одной пары: ход от price до price+spread плюс битые тики.

    Форма важна для отбора. `ramp` — цена идёт от низа к верху, тиков по
    всему диапазону: так выглядит настоящее движение. `saw` — цена скачет
    между границами: так выглядит пара, стоящая на месте в узком коридоре.
    """
    price = Decimal(pair["price"])
    top = price * (1 + Decimal(pair["spread_pct"]) / 100)
    ticks = pair["ticks"]
    step = (until - since) / ticks
    rows = []

    for index in range(ticks):
        if pair["shape"] == "ramp":
            moment_price = price + (top - price) * index / (ticks - 1)
        else:
            moment_price = price if index % 2 else top

        rows.append({
            "symbol": pair["symbol"],
            "last_price": moment_price,
            "quote_asset_volume_24h": pair["volume"],
            "event_time": since + step * index,
        })

    for number, broken_price in enumerate(pair["broken"], start=1):
        rows.append({
            "symbol": pair["symbol"],
            "last_price": Decimal(broken_price),
            "quote_asset_volume_24h": pair["volume"],
            "event_time": until - step * number,
        })

    return rows


INSERT_TICK = """
    INSERT INTO asset_history (
        symbol, source, last_price, quote_asset_volume_24h, event_time
    ) VALUES (
        :symbol, 'BINANCE', :last_price, :quote_asset_volume_24h, :event_time
    )
"""


async def seed(session, now):
    await session.execute(text("TRUNCATE asset_history"))

    rows = []

    for pair in PAIRS:
        rows += build_ticks(pair, since=now - WINDOW, until=now)

    for start in range(0, len(rows), 500):
        await session.execute(text(INSERT_TICK), rows[start:start + 500])

    await session.commit()

    return len(rows)


async def run_checks(session, now):
    since = now - WINDOW

    seeded = await seed(session, now)
    print(f"  засеяно {seeded} тиков по {len(PAIRS)} парам")

    crud = AssetHistoryCrud(session)

    # Сначала — как отбор вёл себя раньше: без отсечек и без отбрасывания
    # крайних тиков побеждает неликвид с битым тиком. Если эта проверка
    # упадёт, значит расклад теста перестал ловить регресс.
    without_cutoffs = await crud.get_most_volatile_since(
        since=since, min_ticks=1, min_ticks_share=0, min_quote_volume_24h=0,
        max_dropped_ticks=0,
    )
    assert without_cutoffs.symbol == "JUNKUSDT", (
        f"без отсечек ожидался мусор, а выбрано {without_cutoffs.symbol}"
    )
    print(f"    без отсечек победил бы {without_cutoffs.symbol} "
          f"(размах {float(without_cutoffs.volatility) * 100:.0f}%)")

    winner = await crud.get_most_volatile_since(since=since)

    assert winner is not None, "отбор не вернул ни одной пары"
    assert winner.symbol == WINNER, (
        f"выбрана {winner.symbol}, а должна была {WINNER}"
    )
    print(f"    выбрана {winner.symbol}: размах "
          f"{float(winner.volatility) * 100:.2f}%, тиков {winner.ticks}, "
          f"оборот {float(winner.quote_volume_24h):,.0f}")

    # Главное: потолка на реальное движение нет. Ход на 25% так и остаётся
    # ходом на 25%, а не срезается до какого-нибудь допустимого коридора.
    assert float(winner.volatility) > 0.2, (
        f"настоящий ход на 25% срезан до "
        f"{float(winner.volatility) * 100:.2f}%"
    )
    print("    настоящее сильное движение не срезано")

    rows = await crud.get_most_volatiles_since(since=since)
    passed = {row.symbol: row for row in rows}

    for symbol in CUT_OFF:
        assert symbol not in passed, f"{symbol} прошла отбор, хотя не должна"

    print(f"    отсечены: {', '.join(CUT_OFF)}")

    # Битые тики не должны надувать размах: у этих пар они дают +50%, а
    # настоящее движение — 0.1%.
    for symbol in FLAT_WITH_GLITCH:
        row = passed.get(symbol)

        assert row is not None, f"{symbol} отсеклась, хотя ликвидна и жива"
        assert float(row.volatility) < 0.01, (
            f"выброс попал в размах {symbol}: "
            f"{float(row.volatility) * 100:.2f}%"
        )
        print(f"    {symbol} посчитана без выброса: "
              f"{float(row.volatility) * 100:.2f}% вместо 50% "
              f"(отброшено тиков с каждой стороны: {row.dropped})")

    # И наоборот: у нормальной пары отбрасывание крайних тиков не должно
    # отъедать движение — 2% обязаны остаться 2%.
    real = passed["REALUSDT"]

    assert 0.019 < float(real.volatility) < 0.021, (
        f"размах REALUSDT поехал: {float(real.volatility) * 100:.2f}%"
    )
    print(f"    REALUSDT осталась при своих "
          f"{float(real.volatility) * 100:.2f}%")

    # Отсечки не должны быть жёстко зашиты: воркер обязан суметь их ослабить,
    # иначе тонкую настройку придётся катить релизом.
    relaxed = await crud.get_most_volatiles_since(
        since=since, min_quote_volume_24h=100_000,
    )

    assert "THINUSDT" in {row.symbol for row in relaxed}, (
        "порог по обороту не читается из аргумента"
    )
    print("    порог по обороту настраивается аргументом")


SLOW_PAIRS = [
    # Медленный питатель: тиков у всех мало, но лучшее из доступного взять
    # надо. Планка по тикам относительная, поэтому отбор не встаёт — LIQUID
    # проходит, а мусор всё равно отсекается по обороту.
    dict(symbol="SLOWLIQUIDUSDT", shape="saw", price="100", spread_pct="1",
         ticks=40, volume=50_000_000, broken=[]),
    dict(symbol="SLOWJUNKUSDT", shape="saw", price="1", spread_pct="20",
         ticks=30, volume=200_000, broken=[]),
    dict(symbol="SLOWDEADUSDT", shape="saw", price="7", spread_pct="30",
         ticks=4, volume=50_000_000, broken=[]),
]


async def check_slow_feed(session, now):
    """При медленном питателе отбор обязан не встать, а взять лучшее из живых.

    Именно на этом ломалась абсолютная планка по частоте тиков: на истории за
    август питатель писал 6-12 тиков в минуту, и любой абсолютный порог
    отсекал вообще все пары — боты стояли бы вместо торговли.
    """
    since = now - WINDOW

    await session.execute(text("TRUNCATE asset_history"))
    rows = []

    for pair in SLOW_PAIRS:
        rows += build_ticks(pair, since=since, until=now)

    await session.execute(text(INSERT_TICK), rows)
    await session.commit()

    crud = AssetHistoryCrud(session)
    winner = await crud.get_most_volatile_since(since=since)

    assert winner is not None, "при медленном питателе отбор встал совсем"
    assert winner.symbol == "SLOWLIQUIDUSDT", (
        f"при медленном питателе выбрана {winner.symbol}"
    )
    print(f"    медленный питатель: выбрана {winner.symbol} "
          f"(тиков всего {winner.ticks})")

    # А пара с четырьмя тиками не проходит даже так: абсолютный минимум
    # остаётся на месте.
    rows = await crud.get_most_volatiles_since(since=since)
    passed = {row.symbol for row in rows}

    assert "SLOWDEADUSDT" not in passed, (
        f"пара с 4 тиками прошла, хотя минимум {MIN_TICKS_IN_WINDOW}"
    )
    assert "SLOWJUNKUSDT" not in passed, "неликвид прошёл по обороту"
    print(f"    и всё равно отсечены: SLOWDEADUSDT (тиков меньше "
          f"{MIN_TICKS_IN_WINDOW}), SLOWJUNKUSDT (оборот)")


async def main():
    print("Отбор волатильной пары:")

    if not os.getenv("VOLATILE_TEST_DB"):
        print(
            "  проверка пропущена: нужна отдельная база.\n"
            "  Как её поднять — в шапке файла."
        )
        return

    print(f"  на живой базе ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        now = datetime.now(UTC)
        await run_checks(session, now=now)
        await check_slow_feed(session, now=now)

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
