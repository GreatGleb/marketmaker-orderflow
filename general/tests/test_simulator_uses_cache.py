"""Сквозная проверка: симулятор берёт цены из кэша, а не из Redis напрямую.

Гоняет один полный цикл simulate_bot на настоящем PriceProvider с кэшем и
следит, чтобы прямых GET по ключам price:* не было ни одного.
"""
import asyncio
import json
from collections import namedtuple
from decimal import Decimal
from tests.price_fixtures import price_snapshot
from unittest.mock import patch

import app.bots.demo_test_bot as m
from tests.strategy_fixtures import LEGACY_STRATEGY_ID
from app.sub_services.watchers.price_provider import PriceCache, PriceProvider

FIELDS = [
    "id", "symbol", "balance", "consider_ma_for_open_order",
    "consider_ma_for_close_order", "ma_number_of_candles_for_open_order",
    "ma_number_of_candles_for_close_order",
    "time_to_wait_for_entry_price_to_open_order_in_seconds",
    "stop_success_ticks", "stop_loss_ticks", "start_updown_ticks",
    "use_trailing_stop", "stop_win_percents", "stop_loss_percents",
    "start_updown_percents", "copybot_v2_time_in_minutes",
    "copy_bot_min_time_profitability_min", "min_timeframe_asset_volatility",
    "copybot_v3_time_in_minutes", "copybot_v3_compound_balance",
    "copybot_v3_stopped_at",
    # Симулятор пишет стратегию парка в каждую сделку, поэтому поле
    # обязано быть у любого конфига, который к нему попадает.
    "strategy_id",
]
Bot = namedtuple("Bot", FIELDS)

BOT = Bot(
    id=1, symbol="BMTUSDT", balance=1000, consider_ma_for_open_order=False,
    consider_ma_for_close_order=False, ma_number_of_candles_for_open_order=5,
    ma_number_of_candles_for_close_order=20,
    time_to_wait_for_entry_price_to_open_order_in_seconds=1,
    stop_success_ticks=20, stop_loss_ticks=30, start_updown_ticks=10,
    use_trailing_stop=False, stop_win_percents=None, stop_loss_percents=None,
    start_updown_percents=None, copybot_v2_time_in_minutes=None,
    copy_bot_min_time_profitability_min=None,
    min_timeframe_asset_volatility=None,
    copybot_v3_time_in_minutes=None, copybot_v3_compound_balance=False,
    copybot_v3_stopped_at=None, strategy_id=LEGACY_STRATEGY_ID,
)

SHARED = {
    "BMTUSDT": {
        "tick_size": Decimal("0.00001"),
        "maker_commission_rate": None,
        "taker_commission_rate": Decimal("0.0005"),
    }
}


class FakeRedis:
    def __init__(self):
        self.store = {"price_snapshot:BMTUSDT": price_snapshot("0.09")}
        self.direct_gets = []
        self.mgets = 0
        self.pushed = []

    async def get(self, key):
        self.direct_gets.append(key)
        return self.store.get(key)

    async def mget(self, keys):
        self.mgets += 1
        return [self.store.get(k) for k in keys]

    async def rpush(self, key, value):
        self.pushed.append(json.loads(value))


async def main():
    redis = FakeRedis()
    cache = PriceCache(redis=redis)
    cache.start()
    provider = PriceProvider(redis=redis, cache=cache)

    stop_event = asyncio.Event()

    async def fake_wait_for(coro, timeout):
        # Не гоняем настоящее ожидание входа: нам важен путь получения цены.
        coro.close()
        stop_event.set()
        return (m.TradeType.BUY.value, Decimal("0.09"))

    with patch.object(m.asyncio, "wait_for", fake_wait_for):
        await m.StartTestBotsCommand(stop_event=stop_event).simulate_bot(
            redis=redis,
            original_bot_config=BOT,
            shared_data=SHARED,
            stop_event=stop_event,
            price_provider=provider,
            binance_bot=None,
        )

    price_gets = [k for k in redis.direct_gets if k.startswith(("price:", "price_snapshot:"))]

    print(f"  сделок записано: {len(redis.pushed)}")
    print(f"  обновлений кэша (MGET): {redis.mgets}")
    print(f"  прямых GET по price:*: {len(price_gets)}")

    assert redis.pushed, "сделка не записалась"
    assert redis.mgets > 0, "кэш ни разу не обновился"
    assert not price_gets, f"симулятор ходил в Redis напрямую: {price_gets[:3]}"

    order = redis.pushed[0]
    assert order["asset_symbol"] == "BMTUSDT"
    assert Decimal(order["open_price"]) == Decimal("0.09")

    print("\nOK: цены идут через кэш, сделка записана")


if __name__ == "__main__":
    asyncio.run(main())
