"""Стратегия 0: детектор прострела, направление входа и выходы.

Заглушки, база и сеть не нужны.

Проверяется то, что отличает эту стратегию от legacy: вход по факту
движения за окно, а не по пробою заранее выставленного уровня; окно
скользящее, поэтому медленный дрейф той же величины прострелом не
считается; уровни выхода процентные, а предел удержания — параметр, а
не общая для всех константа.
"""
import asyncio

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.base import ConfigError, EntryLevels, MarketContext
from app.strategies.strategy_0 import algorithm as s0
from app.strategies.strategy_0.algorithm import (
    ENTRY_CONTINUATION, ENTRY_REVERSAL, Strategy0Algorithm,
)

ALGORITHM = Strategy0Algorithm()
SYMBOL = "BMTUSDT"


def context(config, prices=()):
    """Контекст с ценами, которые отдаются по одной на каждый опрос."""
    queue = list(prices)

    async def get_price(symbol):
        assert symbol == SYMBOL
        return queue.pop(0) if queue else (prices[-1] if prices else None)

    async def before_entry(awaitable):
        return await awaitable

    return MarketContext(
        bot_id=1,
        bot_config=SimpleNamespace(balance=Decimal("1000")),
        symbol=SYMBOL,
        tick_size=Decimal("0.00001"),
        market_data={},
        commission_rate=Decimal("0.0005"),
        price_provider=SimpleNamespace(get_price=get_price),
        price_watcher=None,
        binance_bot=None,
        before_entry=before_entry,
        strategy_config=config,
    )


def check_config():
    config = ALGORITHM.parse_config(None)

    assert config.entry_mode == ENTRY_CONTINUATION, "по умолчанию — по движению"
    assert config.window_seconds > 0 and config.move_percent > 0

    parsed = ALGORITHM.parse_config({
        "schema_version": 1,
        "window_seconds": 3,
        "move_percent": "0.7",
        "entry_mode": ENTRY_REVERSAL,
        "max_hold_seconds": 45,
    })

    assert parsed.window_seconds == 3.0
    assert parsed.move_percent == Decimal("0.7")
    assert parsed.entry_mode == ENTRY_REVERSAL
    assert parsed.max_hold_seconds == 45.0
    # Не заданное остаётся умолчанием, а не обнуляется.
    assert parsed.take_profit_percent > 0

    for bad in (
        {"entry_mode": "sideways"},
        {"move_percent": "0"},
        {"window_seconds": "-1"},
        {"take_profit_percent": "не число"},
        {"опечатка": 1},
        {"schema_version": 99},
    ):
        try:
            ALGORITHM.parse_config(bad)
        except ConfigError:
            continue

        raise AssertionError(f"настройки {bad} приняты, хотя не должны")

    print("  настройки разбираются, мусор и опечатки отвергаются")


async def check_entry_direction():
    config = ALGORITHM.parse_config(
        {"window_seconds": 5, "move_percent": "1.0"}
    )
    levels = EntryLevels(buy=Decimal("0"), sell=Decimal("0"), timeout_seconds=5)

    # Рост на 2% за два опроса: по движению — покупка.
    entry = await ALGORITHM.wait_for_entry(
        context(config, [Decimal("100"), Decimal("102")]), levels
    )
    assert entry.entered and entry.trade_type == TradeType.BUY.value
    assert entry.price == Decimal("102")

    # То же движение при entry_mode=reversal — вход в другую сторону.
    reversal = ALGORITHM.parse_config(
        {"window_seconds": 5, "move_percent": "1.0",
         "entry_mode": ENTRY_REVERSAL}
    )
    entry = await ALGORITHM.wait_for_entry(
        context(reversal, [Decimal("100"), Decimal("102")]), levels
    )
    assert entry.entered and entry.trade_type == TradeType.SELL.value

    # Падение той же величины — зеркально.
    entry = await ALGORITHM.wait_for_entry(
        context(config, [Decimal("100"), Decimal("98")]), levels
    )
    assert entry.entered and entry.trade_type == TradeType.SELL.value

    print("  прострел распознаётся, направление зависит от режима входа")


async def check_no_entry_without_move():
    config = ALGORITHM.parse_config(
        {"window_seconds": 5, "move_percent": "1.0", "max_wait_seconds": 1}
    )
    levels = EntryLevels(buy=Decimal("0"), sell=Decimal("0"), timeout_seconds=1)

    # Движение вдвое меньше порога — не прострел.
    entry = await ALGORITHM.wait_for_entry(
        context(config, [Decimal("100"), Decimal("100.4")]), levels
    )

    assert not entry.entered and entry.timed_out, (
        "движение меньше порога принято за прострел"
    )

    print("  движение меньше порога входом не считается")


async def check_window_slides():
    """Медленный дрейф прострелом не считается.

    Цена та же, что и в проверке входа, но набрана за время, которое
    больше окна: самая старая точка выпадает, и сравнивать оказывается
    не с чем.
    """
    config = ALGORITHM.parse_config(
        {"window_seconds": 1, "move_percent": "1.0", "max_wait_seconds": 1}
    )
    levels = EntryLevels(buy=Decimal("0"), sell=Decimal("0"), timeout_seconds=1)

    moments = iter([0.0, 5.0, 5.1, 5.2, 10.0])

    with patch.object(s0.time, "monotonic", lambda: next(moments, 10.0)):
        entry = await ALGORITHM.wait_for_entry(
            context(config, [Decimal("100"), Decimal("102")]), levels
        )

    assert not entry.entered, "движение за пределами окна принято за прострел"

    print("  окно скользящее: дрейф за его пределами не считается")


async def check_exits():
    config = ALGORITHM.parse_config({
        "take_profit_percent": "1.0",
        "stop_loss_percent": "0.5",
        "max_hold_seconds": 30,
    })
    ctx = context(config)

    position = ALGORITHM.open_position(
        ctx, TradeType.BUY.value, Decimal("100")
    )

    assert position.order.open_price == Decimal("100")
    assert position.state["take_profit_price"] == Decimal("101.0")
    assert position.state["stop_loss_price"] == Decimal("99.5")

    async def decide(price, held):
        return await ALGORITHM.should_exit(
            ctx, position, Decimal(str(price)), held
        )

    assert not (await decide("100.5", 1)).should_exit

    won = await decide("101", 1)
    assert won.should_exit and won.reason == StopReasonEvent.STOP_WON.value

    lost = await decide("99.5", 1)
    assert lost.should_exit and lost.reason == StopReasonEvent.STOP_LOOSED.value

    # За один тик цена прошла оба уровня: считаем худший исход, потому
    # что порядок их достижения внутри тика неизвестен.
    both = await decide("99", 1)
    assert both.reason == StopReasonEvent.STOP_LOOSED.value

    long_lose = await decide("100.2", 31)
    assert long_lose.should_exit
    assert long_lose.reason == StopReasonEvent.STOP_LONG_LOSE.value

    # Для продажи уровни зеркальны.
    short = ALGORITHM.open_position(ctx, TradeType.SELL.value, Decimal("100"))
    assert short.state["take_profit_price"] == Decimal("99.0")
    assert short.state["stop_loss_price"] == Decimal("100.5")

    short_won = await ALGORITHM.should_exit(ctx, short, Decimal("99"), 1)
    assert short_won.reason == StopReasonEvent.STOP_WON.value

    print("  выходы: цель, стоп, предел удержания и зеркальность продажи")


async def main():
    print("Стратегия 0 (прострелы):")
    check_config()
    await check_entry_direction()
    await check_no_entry_without_move()
    await check_window_slides()
    await check_exits()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
