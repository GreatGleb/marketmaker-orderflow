"""Копибот наследует конфиг донора чужой стратегии и торгует им.

Заглушки, база и сеть не нужны:

    python -m tests.test_copybot_strategy_1

Проверяется то, ради чего копиботам открыли пул «любая стратегия»:
копибот — уровень копирования, а не торговая стратегия, и попав на
донора стратегии 1 он обязан торговать **её** алгоритмом, а не своим
парком. В сделке это видно по `executed_strategy_id` и версии
алгоритма: `strategy_id` остаётся стратегией парка копибота.

Отдельно — стык, из-за которого копиботы стратегии 1 не работали вовсе.
Между сигналом и открытием позиции симулятор перепроверяет выбор донора,
и после паузы сверял цену сигнала с текущей. У рыночного входа это
верно, а у лимитного цена входа — цена заявки, и срабатывает заявка
ровно тогда, когда рынок эту цену пробил: проверка не совпадала
никогда, и копибот молча не входил. Здесь она проверяется на живом
цикле, а не на словах.
"""
import asyncio
import json

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import app.bots.demo_test_bot as m

from app.constants.strategy import (
    DONOR_SCOPE_ALL, STRATEGY_1, STRATEGY_LEGACY, donor_scope_list,
)
from app.enums.trade_type import TradeType
from app.strategies.base import EntryResult, signal_price_unchanged
from app.strategies.registry import get_algorithm
from app.strategies.strategy_1.algorithm import STRATEGY_1_ALGORITHM_VERSION
from app.sub_services.logic.donor_selection import donor_payload
from tests.strategy_fixtures import LEGACY_STRATEGY_ID
from tests.test_copybot_v3_simulation import (
    FakeCrud, FakeRedis, FakeSessionManager, MARKET, SYMBOL, V3_BOT,
)
from tests.test_strategy_1_simulation import (
    CONFIG, ENTRY, EXIT_SLIPPAGE, PRICES, Candles, Provider,
)

STRATEGY_1_ID = 3
STRATEGY_KEYS = {
    LEGACY_STRATEGY_ID: STRATEGY_LEGACY,
    STRATEGY_1_ID: STRATEGY_1,
}

COPYBOT_ID = 8001
DONOR_ID = 8101

MAKER = Decimal("0.0002")

# Конфиг донора в том виде, в каком его кладёт в Redis воркер
# (`get_bot_config_by_params`): числа, не строки; тики у бота стратегии
# 1 не заданы вовсе, а все правила лежат в `strategy_config`.
DONOR_CONFIG = {
    "id": DONOR_ID,
    "symbol": SYMBOL,
    "strategy_id": STRATEGY_1_ID,
    "strategy_config": dict(CONFIG),
    "stop_success_ticks": None,
    "stop_loss_ticks": None,
    "start_updown_ticks": None,
    "stop_win_percents": 0.0,
    "stop_loss_percents": 0.0,
    "start_updown_percents": 0.0,
    "min_timeframe_asset_volatility": 0.0,
    "time_to_wait_for_entry_price_to_open_order_in_seconds": 0.0,
    "use_trailing_stop": False,
    "consider_ma_for_open_order": False,
    "consider_ma_for_close_order": False,
    "ma_number_of_candles_for_open_order": 0,
    "ma_number_of_candles_for_close_order": 0,
}


def copybot(donor_scope=DONOR_SCOPE_ALL):
    """Копибот v1 из парка `legacy`: своей пары и своих правил у него нет."""
    return V3_BOT._replace(
        id=COPYBOT_ID,
        symbol='',
        copybot_v3_time_in_minutes=None,
        copybot_v3_compound_balance=False,
        copybot_v2_time_in_minutes=None,
        copy_bot_min_time_profitability_min=Decimal("30"),
        min_timeframe_asset_volatility=None,
        strategy_id=LEGACY_STRATEGY_ID,
        strategy_config=None,
        donor_scope=donor_scope,
    )


async def run_bot(bot, market=None, donor_config=None):
    redis = FakeRedis()
    # Донор опубликован под ключом самого копибота: цепочки у v1 нет.
    redis.store = {
        f"copy_bot_{COPYBOT_ID}": json.dumps(
            donor_payload(donor_config or DONOR_CONFIG)
        ),
    }

    stop = asyncio.Event()
    command = m.StartTestBotsCommand(stop_event=stop)
    command._strategy_keys = dict(STRATEGY_KEYS)

    async def stop_after_sleep(seconds):
        # Минутная пауза отказа означает, что круг кончился ничем:
        # держать тест дальше незачем.
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
            patch.object(m.asyncio, 'sleep',
                         AsyncMock(side_effect=stop_after_sleep)):
        await command.simulate_bot(
            redis, bot, {SYMBOL: dict(market or MARKET)}, stop,
            Provider(PRICES), None, Candles(),
        )

    return redis


async def check_copybot_trades_by_donor_strategy():
    redis = await run_bot(copybot())

    assert redis.pushed, (
        "сделки нет: копибот не проторговал донора стратегии 1. Смотрите "
        "две вещи — пул доноров (`donor_scope`) и проверку сигнала после "
        "`DonorGuard.check(final=True)`"
    )

    trade = redis.pushed[0]

    assert trade["bot_id"] == COPYBOT_ID
    assert trade["referral_bot_id"] == DONOR_ID
    assert trade["donor_chain"] == [DONOR_ID]

    assert trade["strategy_id"] == LEGACY_STRATEGY_ID, (
        "стратегия парка копибота подменена стратегией донора: по ней "
        "потом отбирают ботов парка"
    )
    assert trade["executed_strategy_id"] == STRATEGY_1_ID, (
        f"исполненная стратегия {trade['executed_strategy_id']}: копибот "
        f"записал сделку не тому алгоритму, которым торговал"
    )
    assert trade["algorithm_version"] == STRATEGY_1_ALGORITHM_VERSION, (
        "версия алгоритма взята у парка копибота, а не у донора: сделки "
        "разных алгоритмов сольются в одну статистику"
    )

    assert trade["asset_symbol"] == SYMBOL, (
        "пара из конфига донора не доехала"
    )
    assert trade["order_type"] == TradeType.BUY.value
    assert Decimal(trade["open_price"]) == ENTRY, (
        f"вход {trade['open_price']}, ожидался {ENTRY}: лимитка донора "
        f"исполняется по своей цене"
    )
    assert Decimal(trade["close_price"]) == PRICES[-1] - EXIT_SLIPPAGE, (
        f"закрытие {trade['close_price']}: проскальзывание рыночного "
        f"выхода у копибота считается так же, как у самого донора"
    )

    print("  копибот торгует алгоритмом донора, сделка помечена его стратегией")


async def check_limit_entry_survives_donor_check():
    """Сигнал лимитного входа не отменяется движением цены.

    Проверка отдельная, потому что ломалась она молча: сделок просто не
    было, и выглядело это как «стратегия не находит входов».
    """
    algorithm = get_algorithm(STRATEGY_1)
    signal = EntryResult(trade_type=TradeType.BUY.value, price=ENTRY)
    market_price = PRICES[2]

    assert market_price != ENTRY, (
        "проверка бессмысленна: цена пробоя совпала с ценой заявки"
    )
    assert not signal_price_unchanged(signal, market_price), (
        "общее правило рыночного входа вдруг пропускает пробитую заявку — "
        "тогда и проверять нечего"
    )
    assert await algorithm.entry_still_valid(None, signal, market_price), (
        "стратегия 1 отменяет исполненную заявку из-за того, что цена "
        "ушла за уровень, — а именно так заявка и исполняется"
    )

    print("  лимитный вход не отменяется расхождением с текущей ценой")


async def check_donor_without_strategy_is_refused():
    """Конфиг донора без стратегии — не повод торговать своей.

    Своей стратегии у копибота нет: его работа кончается на выборе
    донора. Раньше здесь стоял откат на стратегию парка, и он был верен,
    пока стратегия была одна. Теперь такой откат объявил бы донора
    `legacy`, взял алгоритм `legacy` и провёл по нему чужие настройки.
    """
    blind = dict(DONOR_CONFIG)
    blind.pop("strategy_id")

    redis = await run_bot(copybot(), donor_config=blind)

    assert not redis.pushed, (
        "копибот проторговал донора, у которого стратегия неизвестна: "
        "значит он подставил свою, и в историю ушла сделка, которой этот "
        "алгоритм не совершал"
    )

    print("  донор без стратегии: круг пропущен, своя не подставлена")


async def check_scope_keeps_foreign_strategy_out():
    """Пул из одной стратегии по-прежнему работает воротами."""
    redis = await run_bot(copybot(donor_scope=donor_scope_list(STRATEGY_LEGACY)))

    assert not redis.pushed, (
        "копибот с пулом только из legacy проторговал донора стратегии 1: "
        "пул перестал быть ограничением, и сузить его станет нечем"
    )

    print("  пул из одной стратегии не пускает чужого донора")


async def check_maker_fee_comes_from_donor_strategy():
    """Ставка входа берётся по признаку алгоритма донора, а не копибота."""
    redis = await run_bot(copybot(), market=dict(MARKET, maker_commission_rate=MAKER))
    trade = redis.pushed[0]

    balance = Decimal("1000")
    amount = balance / Decimal(trade["open_price"])

    assert Decimal(trade["open_fee"]) == (
        amount * Decimal(trade["open_price"]) * MAKER
    ), (
        f"комиссия входа {trade['open_fee']} посчитана не по ставке maker: "
        f"`entry_is_maker` берётся у алгоритма донора"
    )
    assert Decimal(trade["open_fee"]) < Decimal(trade["close_fee"])

    print("  комиссия входа — maker, как у самого донора")


async def main():
    print("Копибот на доноре чужой стратегии:")
    await check_limit_entry_survives_donor_check()
    await check_copybot_trades_by_donor_strategy()
    await check_maker_fee_comes_from_donor_strategy()
    await check_scope_keeps_foreign_strategy_out()
    await check_donor_without_strategy_is_refused()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
