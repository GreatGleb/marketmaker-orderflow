"""Бот стратегии 0 проходит цикл симулятора и пишет сделку.

Заглушки, база и сеть не нужны.

Это проверка того, ради чего затевался весь рефакторинг: новая
стратегия должна работать без единой правки в общем цикле. Поэтому
здесь не вызываются методы алгоритма по одному — гоняется настоящий
`simulate_bot`, и сделка проверяется такой, какой она уходит в очередь:
с исполненной стратегией и версией её алгоритма.
"""
import asyncio
import json

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import app.bots.demo_test_bot as m

from app.constants.strategy import STRATEGY_0
from app.enums.event_type import StopReasonEvent
from app.enums.trade_type import TradeType
from app.strategies.strategy_0.algorithm import STRATEGY_0_ALGORITHM_VERSION
from tests.test_copybot_v3_simulation import (
    FakeCrud, FakeRedis, FakeSessionManager, MARKET, SYMBOL, V3_BOT,
)

STRATEGY_0_ID = 2
STRATEGY_KEYS = {STRATEGY_0_ID: STRATEGY_0}

CONFIG = {
    "schema_version": 1,
    "window_seconds": 5,
    "move_percent": "1.0",
    "entry_mode": "continuation",
    "take_profit_percent": "1.0",
    "stop_loss_percent": "1.0",
    "max_hold_seconds": 30,
    "max_wait_seconds": 5,
}

# Цена стоит, потом простреливает вверх на 2% — это вход по движению.
# Дальше растёт до цели: 102 + 1% = 103.02.
PRICES = [
    Decimal("100"), Decimal("100"), Decimal("102"),
    Decimal("102.5"), Decimal("103.1"),
]


def bot():
    return V3_BOT._replace(
        id=5001,
        symbol=SYMBOL,
        copybot_v3_time_in_minutes=None,
        copybot_v3_compound_balance=False,
        copy_bot_min_time_profitability_min=None,
        copybot_v2_time_in_minutes=None,
        min_timeframe_asset_volatility=None,
        strategy_id=STRATEGY_0_ID,
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


async def run_bot():
    redis = FakeRedis()
    stop = asyncio.Event()
    command = m.StartTestBotsCommand(stop_event=stop)
    command._strategy_keys = dict(STRATEGY_KEYS)

    async def stop_after_sleep(seconds):
        # Короткие паузы — такт цикла удержания, их пропускаем. Длинная
        # означает, что бот ушёл на новый круг ни с чем.
        if seconds >= 1:
            stop.set()

    pushed = redis.rpush

    async def rpush_once(*args, **kwargs):
        # Сделка записана — на этом проверка закончена. Без явной
        # остановки бот пошёл бы на следующий круг и ждал бы там нового
        # прострела до `max_wait_seconds`, а цены в заглушке кончились.
        result = await pushed(*args, **kwargs)
        stop.set()
        return result

    redis.rpush = rpush_once

    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', FakeCrud), \
            patch.object(m.asyncio, 'sleep', AsyncMock(side_effect=stop_after_sleep)):
        await command.simulate_bot(
            redis, bot(), {SYMBOL: dict(MARKET)}, stop,
            Provider(PRICES), None,
        )

    return redis


async def check_trade_written():
    redis = await run_bot()

    assert redis.pushed, (
        "сделки нет: бот стратегии 0 не прошёл цикл симулятора"
    )

    trade = redis.pushed[0]

    if isinstance(trade, (str, bytes)):
        trade = json.loads(trade)

    assert trade["bot_id"] == 5001
    assert trade["asset_symbol"] == SYMBOL
    assert trade["order_type"] == TradeType.BUY.value, (
        "вход по движению вверх должен быть покупкой"
    )
    assert Decimal(trade["open_price"]) == Decimal("102"), trade["open_price"]

    assert trade["strategy_id"] == STRATEGY_0_ID
    assert trade["executed_strategy_id"] == STRATEGY_0_ID, (
        "у обычного бота исполненная стратегия совпадает со стратегией парка"
    )
    assert trade["algorithm_version"] == STRATEGY_0_ALGORITHM_VERSION, (
        "версия чужого алгоритма в сделке: по ней потом сравнивают результаты"
    )
    assert trade["donor_chain"] is None, "обычный бот доноров не имеет"
    assert trade["stop_reason_event"] == StopReasonEvent.STOP_WON.value

    # Уровни в тиках стратегия не использует — в сделке нули, а не мусор.
    assert trade["start_updown_ticks"] == 0
    assert trade["stop_loss_ticks"] == 0

    print("  сделка записана: стратегия, версия алгоритма и причина выхода")


async def main():
    print("Стратегия 0 в цикле симулятора:")
    await check_trade_written()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
