"""Бот стратегии 1 проходит цикл симулятора и пишет сделку.

Заглушки, база и сеть не нужны.

Гоняется настоящий `simulate_bot`: новая стратегия должна работать без
правок общего цикла. Проверяется и то, чего не было у прежних стратегий
— комиссия открытия по ставке maker, потому что вход лимитный.
"""
import asyncio
import json

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.bots.demo_test_bot as m

from app.constants.strategy import STRATEGY_1
from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.strategy_1.algorithm import STRATEGY_1_ALGORITHM_VERSION
from tests.test_copybot_v3_simulation import (
    FakeCrud, FakeRedis, FakeSessionManager, MARKET, SYMBOL, V3_BOT,
)

STRATEGY_1_ID = 3
STRATEGY_KEYS = {STRATEGY_1_ID: STRATEGY_1}

MAKER = Decimal("0.0002")
TAKER = MARKET["taker_commission_rate"]
ATR_PERCENT = Decimal("1.0")

CONFIG = {
    "schema_version": 1,
    "atr_period": 14,
    "entry_offset_atr": "2",
    "take_profit_fraction": "0.5",
    "stop_loss_atr": "3",
    "time_stop_seconds": 300,
    "max_wait_seconds": 5,
    "side": "both",
    "min_atr_percent": "0.05",
}

# Цена 100, ATR 1%, отступ 2 ATR — заявка на покупку стоит на 98.
# Вынос вниз до 97.5 пробивает её (нужно минимум на тик), исполнение
# считается по цене заявки: 98. Цель — половина отступа от входа:
# 98 + 98 × 1% × 2 × 0.5 = 98.98. Последняя цена её перекрывает.
PRICES = [
    Decimal("100"), Decimal("99"), Decimal("97.5"),
    Decimal("98.5"), Decimal("99.10"),
]

ENTRY = Decimal("98")
# Проскальзывание выхода: 98 × 1% × 0.1 / 100 — доля ATR от цены входа.
EXIT_SLIPPAGE = ENTRY * ATR_PERCENT * Decimal("0.1") / 100


def bot():
    return V3_BOT._replace(
        id=7001,
        symbol=SYMBOL,
        copybot_v3_time_in_minutes=None,
        copybot_v3_compound_balance=False,
        copy_bot_min_time_profitability_min=None,
        copybot_v2_time_in_minutes=None,
        min_timeframe_asset_volatility=None,
        strategy_id=STRATEGY_1_ID,
        strategy_config=dict(CONFIG),
        donor_scope=None,
    )


class Provider:
    """Цены по списку; последняя повторяется, пока её спрашивают."""

    def __init__(self, prices):
        self.queue = list(prices)
        self.last = prices[-1]

    async def get_price(self, symbol):
        assert symbol == SYMBOL

        if self.queue:
            self.last = self.queue.pop(0)

        return self.last

    async def _read_price(self, symbol):
        return self.last


class Candles:
    """Постоянный ATR: проверяется стратегия, а не расчёт волатильности."""

    async def get_atr_percent(self, symbol, period):
        assert symbol == SYMBOL

        return ATR_PERCENT

    async def get_atr(self, symbol, period):
        return ATR_PERCENT

    async def get_candles(self, symbol):
        return []


async def run_bot(market=None):
    redis = FakeRedis()
    stop = asyncio.Event()
    command = m.StartTestBotsCommand(stop_event=stop)
    command._strategy_keys = dict(STRATEGY_KEYS)

    async def stop_after_sleep(seconds):
        if seconds >= 1:
            stop.set()

    pushed = redis.rpush

    async def rpush_once(*args, **kwargs):
        result = await pushed(*args, **kwargs)
        stop.set()
        return result

    redis.rpush = rpush_once

    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', FakeCrud), \
            patch.object(m.asyncio, 'sleep', AsyncMock(side_effect=stop_after_sleep)):
        await command.simulate_bot(
            redis, bot(), {SYMBOL: dict(market or MARKET)}, stop,
            Provider(PRICES), None, Candles(),
        )

    return redis


def trade_of(redis):
    assert redis.pushed, (
        "сделки нет: бот стратегии 1 не прошёл цикл симулятора"
    )

    trade = redis.pushed[0]

    return json.loads(trade) if isinstance(trade, (str, bytes)) else trade


async def check_trade_written():
    trade = trade_of(await run_bot())

    assert trade["bot_id"] == 7001
    assert trade["order_type"] == TradeType.BUY.value, (
        "провал до нижней заявки — это покупка"
    )
    assert Decimal(trade["open_price"]) == ENTRY, (
        f"вход {trade['open_price']}: лимитка исполняется по своей цене, "
        f"а не по цене, до которой рынок проскочил"
    )
    assert Decimal(trade["close_price"]) == Decimal("99.10") - EXIT_SLIPPAGE, (
        f"закрытие {trade['close_price']}: рыночный выход должен учитывать "
        f"проскальзывание"
    )

    assert trade["strategy_id"] == STRATEGY_1_ID
    assert trade["executed_strategy_id"] == STRATEGY_1_ID
    assert trade["algorithm_version"] == STRATEGY_1_ALGORITHM_VERSION
    assert trade["donor_chain"] is None
    assert trade["stop_reason_event"] == StopReasonEvent.STOP_WON.value

    # Тиковых уровней у стратегии нет — в сделке нули, а не мусор.
    assert trade["start_updown_ticks"] == 0
    assert trade["stop_loss_ticks"] == 0

    print("  сделка записана: вход лимиткой, выход по возврату")


async def check_maker_fee_on_entry():
    """Вход лимитный: комиссия открытия ниже комиссии закрытия."""
    market = dict(MARKET, maker_commission_rate=MAKER)
    trade = trade_of(await run_bot(market))

    balance = Decimal("1000")
    amount = balance / Decimal(trade["open_price"])

    expected_open = amount * Decimal(trade["open_price"]) * MAKER
    expected_close = amount * Decimal(trade["close_price"]) * TAKER

    assert Decimal(trade["open_fee"]) == expected_open, (
        f"комиссия входа {trade['open_fee']} посчитана не по ставке maker"
    )
    assert Decimal(trade["close_fee"]) == expected_close, (
        "выход рыночный: комиссия закрытия остаётся taker"
    )
    assert Decimal(trade["open_fee"]) < Decimal(trade["close_fee"])

    print("  комиссия входа — maker, комиссия выхода — taker")


async def check_without_maker_rate():
    """Ставка maker не засеяна — считаем по общей, а не падаем."""
    trade = trade_of(await run_bot())

    amount = Decimal("1000") / Decimal(trade["open_price"])
    assert Decimal(trade["open_fee"]) == (
        amount * Decimal(trade["open_price"]) * TAKER
    )

    print("  без ставки maker вход считается по общей ставке")


async def main():
    print("Стратегия 1 в цикле симулятора:")
    await check_trade_written()
    await check_maker_fee_on_entry()
    await check_without_maker_rate()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
