"""Стратегия 1: отступ в ATR, вход лимиткой, выход на возврате.

Заглушки, база и сеть не нужны.

Проверяется то, что отличает её от стратегии 0: заявка стоит заранее и
против движения, отступ и уровни считаются от ATR пары, комиссия входа
берётся по ставке maker, а без ATR стратегия не входит вовсе.
"""
import asyncio

from decimal import Decimal
from types import SimpleNamespace

from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.base import ConfigError, MarketContext
from app.strategies.strategy_1.algorithm import (
    SIDE_LONG, SIDE_SHORT, Strategy1Algorithm,
)

ALGORITHM = Strategy1Algorithm()
SYMBOL = "ONEUSDT"


def context(config, prices=(), atr_percent=Decimal("1.0"),
            maker=Decimal("0.0002"), taker=Decimal("0.0005")):
    queue = list(prices)

    async def get_price(symbol):
        assert symbol == SYMBOL
        return queue.pop(0) if queue else (prices[-1] if prices else None)

    async def before_entry(awaitable):
        return await awaitable

    async def get_atr_percent(symbol, period):
        assert symbol == SYMBOL
        return atr_percent

    return MarketContext(
        bot_id=1,
        bot_config=SimpleNamespace(balance=Decimal("1000")),
        symbol=SYMBOL,
        tick_size=Decimal("0.00001"),
        market_data={},
        commission_rate=taker,
        price_provider=SimpleNamespace(get_price=get_price),
        price_watcher=None,
        binance_bot=None,
        before_entry=before_entry,
        candle_provider=SimpleNamespace(get_atr_percent=get_atr_percent),
        maker_commission_rate=maker,
        strategy_config=config,
    )


def test_config_defaults_and_validation():
    config = ALGORITHM.parse_config(None)

    assert config.entry_offset_atr > 0 and config.stop_loss_atr > 0
    assert config.take_profit_fraction < 1, "цель — часть отступа, не весь он"

    for broken in ({"take_profit_fraction": "1.0"}, {"side": "up"},
                   {"entry_offset_atr": "0"}, {"unknown": 1},
                   {"schema_version": 2}):
        try:
            ALGORITHM.parse_config(broken)
        except ConfigError:
            continue

        raise AssertionError(f"настройки {broken} должны быть отвергнуты")


async def test_levels_stand_against_the_move():
    """Заявки стоят по обе стороны, на отступе в ATR от цены."""
    config = ALGORITHM.parse_config({"entry_offset_atr": "2"})
    ctx = context(config, atr_percent=Decimal("1.0"))

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))

    # ATR 1% × множитель 2 = 2% от цены в каждую сторону.
    assert levels.buy == Decimal("98")
    assert levels.sell == Decimal("102")
    # ATR, по которому посчитан отступ, запомнен для открытия позиции.
    assert ctx.strategy_state["atr_percent"] == Decimal("1.0")


async def test_no_atr_no_entry():
    """Без ATR заявки не выставляются и вход не происходит."""
    config = ALGORITHM.parse_config({"max_wait_seconds": "0.3"})
    ctx = context(config, prices=[Decimal("100")] * 10, atr_percent=None)

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))
    assert levels.buy == levels.sell == Decimal("100")

    result = await ALGORITHM.wait_for_entry(ctx, levels)
    assert result.timed_out and not result.entered


async def test_low_volatility_is_skipped():
    """Пара тише порога не торгуется: цель не покроет комиссию."""
    config = ALGORITHM.parse_config({"min_atr_percent": "0.5"})
    ctx = context(config, atr_percent=Decimal("0.1"))

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))

    assert levels.buy == levels.sell


async def test_buy_fills_on_dip():
    """Провал ниже заявки — это покупка, и по цене самой заявки.

    Правила исполнения (пробитие против касания) проверяются отдельно —
    `tests/test_strategy_1_execution.py`.
    """
    config = ALGORITHM.parse_config({"entry_offset_atr": "2"})
    # Цена валится ниже 98 — туда, где стоит заявка на покупку.
    ctx = context(config, prices=[Decimal("100"), Decimal("99"), Decimal("97.5")])

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))
    result = await ALGORITHM.wait_for_entry(ctx, levels)

    assert result.entered
    assert result.trade_type == TradeType.BUY.value
    assert result.price == Decimal("98"), (
        "исполнение по цене заявки: проскочившая цена достаётся тем, кто "
        "стоял в очереди раньше"
    )


async def test_side_limits_direction():
    """При side=long вынос вверх не открывает шорт."""
    config = ALGORITHM.parse_config({
        "entry_offset_atr": "2", "side": SIDE_LONG, "max_wait_seconds": "0.3",
    })
    ctx = context(config, prices=[Decimal("100"), Decimal("103"), Decimal("104")])

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))
    result = await ALGORITHM.wait_for_entry(ctx, levels)

    assert result.timed_out, "шорт запрещён настройкой"


async def test_short_fills_on_spike():
    config = ALGORITHM.parse_config({"entry_offset_atr": "2", "side": SIDE_SHORT})
    ctx = context(config, prices=[Decimal("100"), Decimal("102.5")])

    levels = await ALGORITHM.entry_levels(ctx, Decimal("100"))
    result = await ALGORITHM.wait_for_entry(ctx, levels)

    assert result.trade_type == TradeType.SELL.value


async def test_position_targets_part_of_the_offset():
    """Цель — доля отступа, стоп — от цены входа, комиссия входа maker."""
    config = ALGORITHM.parse_config({
        "entry_offset_atr": "2", "take_profit_fraction": "0.5",
        "stop_loss_atr": "3",
    })
    ctx = context(config, atr_percent=Decimal("1.0"))
    await ALGORITHM.entry_levels(ctx, Decimal("100"))

    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    # Отступ от цены входа: 98 × 1% × 2 = 1.96; половина от него — цель.
    assert position.state["take_profit_price"] == Decimal("98") + Decimal("0.98")
    # Стоп: 98 × 1% × 3 = 2.94 ниже входа.
    assert position.state["stop_loss_price"] == Decimal("98") - Decimal("2.94")
    # Вход лимитный: комиссия по ставке maker, а не taker.
    assert position.order.open_fee == Decimal("1000") * Decimal("0.0002")


async def test_exit_rules():
    config = ALGORITHM.parse_config({
        "entry_offset_atr": "2", "take_profit_fraction": "0.5",
        "stop_loss_atr": "3", "time_stop_seconds": "15",
    })
    ctx = context(config, atr_percent=Decimal("1.0"))
    await ALGORITHM.entry_levels(ctx, Decimal("100"))
    position = ALGORITHM.open_position(ctx, TradeType.BUY.value, Decimal("98"))

    take = position.state["take_profit_price"]
    stop = position.state["stop_loss_price"]

    won = await ALGORITHM.should_exit(ctx, position, take, held_seconds=1)
    assert won.should_exit and won.reason == StopReasonEvent.STOP_WON.value

    lost = await ALGORITHM.should_exit(ctx, position, stop, held_seconds=1)
    assert lost.should_exit and lost.reason == StopReasonEvent.STOP_LOOSED.value

    # За один тик пройдены оба уровня — засчитывается худший исход.
    both = await ALGORITHM.should_exit(ctx, position, stop, held_seconds=100)
    assert both.reason == StopReasonEvent.STOP_LOOSED.value

    # Возврата не случилось: выход по времени, а не ожидание тренда.
    waited = await ALGORITHM.should_exit(
        ctx, position, Decimal("98.1"), held_seconds=15
    )
    assert waited.should_exit
    assert waited.reason == StopReasonEvent.STOP_LONG_LOSE.value

    holding = await ALGORITHM.should_exit(
        ctx, position, Decimal("98.1"), held_seconds=1
    )
    assert not holding.should_exit


def test_maker_flag_is_declared():
    """Симулятор узнаёт про лимитный вход по этому признаку."""
    assert Strategy1Algorithm.entry_is_maker is True


def main():
    test_config_defaults_and_validation()
    asyncio.run(test_levels_stand_against_the_move())
    asyncio.run(test_no_atr_no_entry())
    asyncio.run(test_low_volatility_is_skipped())
    asyncio.run(test_buy_fills_on_dip())
    asyncio.run(test_side_limits_direction())
    asyncio.run(test_short_fills_on_spike())
    asyncio.run(test_position_targets_part_of_the_offset())
    asyncio.run(test_exit_rules())
    test_maker_flag_is_declared()
    print("strategy_1: ок")


if __name__ == "__main__":
    main()
