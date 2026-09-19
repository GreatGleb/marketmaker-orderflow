"""Прогон сделки копибота v3 через симулятор, от отбора донора до записи.

Остальные проверки v3 разбирают части по отдельности: `test_copybot_v3_chain`
— отбор донора, `test_copybot_v3_compound` — арифметику размера позиции. Здесь
`simulate_bot` проходит весь путь целиком, потому что между этими частями есть
стык, который больше ничем не проверяется.

Стык такой: в `test_orders` у компаундирующего бота должен уехать **номинал
позиции**, а не остаток счёта. Количество округлено по шагу лота, и
`calculate_pnl` внутри делает `amount = balance / open_price` — передай туда
счёт, и прибыль посчиталась бы по дробному количеству, которого на бирже не
бывает. Ошибка такого рода ничего не роняет: сделки пишутся, отчёт рисуется,
просто цифры неверные.

Заглушки, база не нужна:

    python -m tests.test_copybot_v3_simulation
"""
import asyncio
import json

from collections import namedtuple
from decimal import Decimal
from app.sub_services.logic.donor_selection import donor_payload
from tests.price_fixtures import price_snapshot
from unittest.mock import patch

import app.bots.demo_test_bot as m
from tests.strategy_fixtures import (
    LEGACY_STRATEGY_ID, execute_strategy_maps,
)

from app.db.models import TestBot
from app.sub_services.logic.price_calculator import PriceCalculator
from app.sub_services.watchers.price_provider import PriceCache, PriceProvider

SYMBOL = "TESTUSDT"
PRICE = Decimal("100")
COMMISSION = Decimal("0.001")

V3_BOT_ID, COPYBOT_V1_ID, COPYBOT_V2_ID, DONOR_ID = 900, 201, 301, 101

# Круглые числа, чтобы ожидаемый номинал считался в уме: 1000 * 0.99 / 100 =
# 9.9 — ровно по шагу 0.1, и номинал 990.
MARKET = {
    "tick_size": Decimal("0.01"),
    "maker_commission_rate": None,
    "taker_commission_rate": COMMISSION,
    "step_size": Decimal("0.1"),
    "market_step_size": Decimal("0.1"),
    "market_min_qty": Decimal("1"),
    "market_max_qty": Decimal("100000"),
    "min_notional": Decimal("5"),
    "min_qty": Decimal("1"),
    "max_qty": Decimal("1000"),
    "min_price": Decimal("0.01"),
    "max_price": Decimal("100000"),
}

EXPECTED_QUANTITY = Decimal("9.9")
EXPECTED_NOTIONAL = EXPECTED_QUANTITY * PRICE

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

V3_BOT = Bot(
    id=V3_BOT_ID, symbol='', balance=Decimal("1000"),
    consider_ma_for_open_order=False, consider_ma_for_close_order=False,
    ma_number_of_candles_for_open_order=0,
    ma_number_of_candles_for_close_order=0,
    time_to_wait_for_entry_price_to_open_order_in_seconds=1,
    stop_success_ticks=20, stop_loss_ticks=30, start_updown_ticks=1,
    use_trailing_stop=False, stop_win_percents=None, stop_loss_percents=None,
    start_updown_percents=None, copybot_v2_time_in_minutes=None,
    copy_bot_min_time_profitability_min=None,
    min_timeframe_asset_volatility=None,
    copybot_v3_time_in_minutes=Decimal("720"),
    copybot_v3_compound_balance=True,
    copybot_v3_stopped_at=None, strategy_id=LEGACY_STRATEGY_ID,
)

# Конфиг донора в том виде, в каком его кладёт воркер: числа, не строки.
DONOR_CONFIG = {
    "id": DONOR_ID,
    "symbol": SYMBOL,
    "stop_success_ticks": 20,
    "stop_loss_ticks": 30,
    "start_updown_ticks": 1,
    "stop_win_percents": 0.0,
    "stop_loss_percents": 0.0,
    "start_updown_percents": 0.0,
    "min_timeframe_asset_volatility": 0.0,
    "time_to_wait_for_entry_price_to_open_order_in_seconds": 1.0,
    "use_trailing_stop": False,
    "consider_ma_for_open_order": False,
    "consider_ma_for_close_order": False,
    "ma_number_of_candles_for_open_order": 0,
    "ma_number_of_candles_for_close_order": 0,
}

LADDER = {
    COPYBOT_V1_ID: TestBot(
        id=COPYBOT_V1_ID, symbol='', balance=Decimal("1000"),
        copy_bot_min_time_profitability_min=Decimal("30"),
    ),
    COPYBOT_V2_ID: TestBot(
        id=COPYBOT_V2_ID, symbol='', balance=Decimal("1000"),
        copybot_v2_time_in_minutes=Decimal("60"),
    ),
}


class FakeCrud:
    """Лестница копиботов плюс запись состояния бота v3."""

    saved_balances = []
    stopped = []

    def __init__(self, session=None):
        pass

    async def get_sorted_by_profit(
        self, since, just_copy_bots=False, just_copy_bots_v2=False, **kw
    ):
        if just_copy_bots_v2:
            return [(COPYBOT_V2_ID, Decimal("7"), 10, 5)]

        if just_copy_bots:
            return [(COPYBOT_V1_ID, Decimal("5"), 10, 5)]

        return []

    async def get_bot_by_id(self, bot_id):
        bot = LADDER.get(bot_id)

        return [bot] if bot else []

    async def strategy_id_by_bot(self):
        return {bot_id: LEGACY_STRATEGY_ID for bot_id in LADDER}

    async def set_balance(self, bot_id, balance):
        FakeCrud.saved_balances.append((bot_id, balance))

    async def mark_v3_stopped(self, bot_id, stopped_at=None):
        FakeCrud.stopped.append(bot_id)


class FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        # Карту стратегий читают и симулятор, и воркер публикации
        # доноров — обоим хватает этой сессии.
        return execute_strategy_maps(statement)


class FakeSessionManager:
    @staticmethod
    def create(url):
        return FakeSessionManager()

    def get_session(self):
        return FakeSession()


class FakeRedis:
    def __init__(self):
        self.store = {
            f"price_snapshot:{SYMBOL}": price_snapshot(str(PRICE)),
            f"copy_bot_{COPYBOT_V1_ID}": json.dumps(donor_payload(DONOR_CONFIG)),
        }
        self.pushed = []

    async def get(self, key):
        return self.store.get(key)

    async def mget(self, keys):
        return [self.store.get(k) for k in keys]

    async def rpush(self, key, value):
        self.pushed.append(json.loads(value))


async def run_once(bot, market=None):
    """Один проход `simulate_bot` на заглушках. Возвращает FakeRedis."""
    redis = FakeRedis()
    cache = PriceCache(redis=redis)
    cache.start()
    provider = PriceProvider(redis=redis, cache=cache)

    stop_event = asyncio.Event()

    async def fake_wait_for(coro, timeout):
        coro.close()
        stop_event.set()
        return (m.TradeType.BUY.value, PRICE)

    real_sleep = asyncio.sleep

    async def fake_sleep(seconds):
        # Пропускаем только длинные паузы отказа (минута), ради которых иначе весь
        # `tests.run_all` шёл бы на две минуты дольше. Короткие трогать
        # нельзя: на них держится фоновое обновление PriceCache, и без
        # задержки его цикл превращается в busy-loop, который не отдаёт
        # управление остальным корутинам.
        if seconds >= 1:
            return None

        return await real_sleep(seconds)

    command = m.StartTestBotsCommand(stop_event=stop_event)

    with patch.object(m.asyncio, "wait_for", fake_wait_for), \
            patch.object(m.asyncio, "sleep", fake_sleep), \
            patch.object(m, "TestBotCrud", FakeCrud), \
            patch.object(m, "DatabaseSessionManager", FakeSessionManager):
        await command.simulate_bot(
            redis=redis,
            original_bot_config=bot,
            shared_data={SYMBOL: dict(market or MARKET)},
            stop_event=stop_event,
            price_provider=provider,
            binance_bot=None,
        )

    return redis, command


async def check_one_bad_pair_does_not_stop_the_bot():
    """Пара, на которой лот не набрался, не обрывает прогноз.

    Копибот торгует парой донора, донор меняется от сделки к сделке. Если
    остановиться по первой же неудаче, прогноз оборвётся из-за одной
    экзотической пары — и в отчёте это будет выглядеть как настоящий слив
    счёта, то есть ровно наоборот тому, что произошло.
    """
    FakeCrud.stopped.clear()
    FakeCrud.saved_balances.clear()

    # minQty такая, что на тысячу не набирается ни при какой цене.
    unreachable = dict(MARKET, min_qty=Decimal("10000"), max_qty=Decimal("100000"))

    redis, command = await run_once(V3_BOT, market=unreachable)

    assert not redis.pushed, (
        "сделка записана, хотя лот не набрался — в прогноз попал ордер, "
        "который биржа не приняла бы"
    )
    assert not FakeCrud.stopped, (
        "бот остановлен навсегда после одной неудачной пары: прогноз "
        "оборвётся там, где реальный бот просто взял бы следующего донора"
    )

    shortages = command._v3_shortages.get(V3_BOT_ID)

    assert shortages == 1, (
        f"учтено {shortages} неудач вместо одной — счётчик до остановки "
        f"({m.StartTestBotsCommand.COMPOUND_SHORTAGE_LIMIT}) пойдёт не в том "
        f"темпе"
    )

    print(
        f"  нехватка учтена ({shortages} из "
        f"{m.StartTestBotsCommand.COMPOUND_SHORTAGE_LIMIT}), бот жив"
    )


async def check_missing_specs_do_not_count_as_shortage():
    """Дырка в справочнике пар не приближает остановку."""
    FakeCrud.stopped.clear()

    blank = dict(MARKET)
    blank["step_size"] = None

    redis, command = await run_once(V3_BOT, market=blank)

    assert not redis.pushed, "сделка записана без шага лота"
    assert not FakeCrud.stopped, "бот остановлен из-за незасеянных спеков"
    assert not command._v3_shortages.get(V3_BOT_ID), (
        "отсутствие спеков засчитано как нехватка средств: достаточно "
        "одной незасеянной пары подряд, и бот умрёт ни за что"
    )

    print("  незасеянные спеки: пропуск, счётчик нехваток не тронут")


async def main():
    print("Прогоняем сделку копибота v3 через симулятор")

    redis = FakeRedis()
    cache = PriceCache(redis=redis)
    cache.start()
    provider = PriceProvider(redis=redis, cache=cache)

    stop_event = asyncio.Event()

    async def fake_wait_for(coro, timeout):
        # Настоящее ожидание пробоя здесь не нужно: проверяется путь от
        # отбора донора до записи сделки, а не срабатывание уровня.
        coro.close()
        stop_event.set()
        return (m.TradeType.BUY.value, PRICE)

    command = m.StartTestBotsCommand(stop_event=stop_event)

    with patch.object(m.asyncio, "wait_for", fake_wait_for), \
            patch.object(m, "TestBotCrud", FakeCrud), \
            patch.object(m, "DatabaseSessionManager", FakeSessionManager):
        await command.simulate_bot(
            redis=redis,
            original_bot_config=V3_BOT,
            shared_data={SYMBOL: dict(MARKET)},
            stop_event=stop_event,
            price_provider=provider,
            binance_bot=None,
        )

    assert redis.pushed, (
        "сделка не записалась: ветка v3 не дошла до конца — проверьте, что "
        "спуск v3 → v2 → v1 нашёл донора и конфиг прочитался из Redis"
    )

    order = redis.pushed[0]

    print(f"  пара из конфига донора: {order['asset_symbol']}")
    print(f"  донор в сделке: {order['referral_bot_id']}")

    assert order["asset_symbol"] == SYMBOL, (
        f"торговали парой {order['asset_symbol']}: конфиг донора не доехал"
    )
    assert order["referral_bot_id"] == DONOR_ID, (
        f"в сделке донор {order['referral_bot_id']}, ожидался {DONOR_ID}"
    )

    balance = Decimal(order["balance"])

    print(f"  в сделку уехал номинал: {balance}")

    assert balance == EXPECTED_NOTIONAL, (
        f"в test_orders уехало {balance}, ожидался номинал "
        f"{EXPECTED_NOTIONAL} ({EXPECTED_QUANTITY} по цене {PRICE}). Если "
        f"там 1000 — значит в расчёт ушёл счёт, и PnL посчитан по дробному "
        f"количеству, которого на бирже не бывает"
    )

    expected_pnl = PriceCalculator.calculate_pnl(
        trade_type=m.TradeType.BUY.value,
        balance=EXPECTED_NOTIONAL,
        open_price=PRICE,
        close_price=PRICE,
        commission_open=COMMISSION,
        commission_close=COMMISSION,
    )

    assert Decimal(order["profit_loss"]) == expected_pnl, (
        f"PnL {order['profit_loss']}, ожидался {expected_pnl}"
    )

    fees = Decimal(order["open_fee"]) + Decimal(order["close_fee"])

    assert Decimal(order["profit_loss"]) == -fees, (
        f"цены входа и выхода равны, значит весь результат — комиссии, но "
        f"profit_loss {order['profit_loss']} не равен -{fees}: поля ордера "
        f"и PnL посчитаны от разных величин"
    )

    print(f"  PnL {expected_pnl} — это комиссии {fees}")

    assert FakeCrud.saved_balances, (
        "счёт не сохранён в test_bots: после перезапуска симулятора кривая "
        "начнётся заново, и прогноз порвётся молча"
    )

    bot_id, saved = FakeCrud.saved_balances[-1]

    assert bot_id == V3_BOT_ID, f"счёт записан не тому боту: {bot_id}"
    assert saved == Decimal("1000") + expected_pnl, (
        f"сохранён счёт {saved}, ожидался {Decimal('1000') + expected_pnl} — "
        f"прибыль не реинвестируется"
    )

    print(f"  счёт записан: 1000 → {saved}")

    assert not FakeCrud.stopped, (
        "бот остановлен на сделке, которая прошла нормально"
    )

    print("✅ путь v3 → v2 → v1 → донор пройден, номинал и счёт сходятся")

    await check_one_bad_pair_does_not_stop_the_bot()
    await check_missing_specs_do_not_count_as_shortage()

    print("✅ отказы по паре не обрывают прогноз")


if __name__ == "__main__":
    asyncio.run(main())
