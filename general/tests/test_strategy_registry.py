"""Реестр стратегий и отказ работать с незарегистрированной.

Заглушки, база не нужна.

Главное здесь — поведение при неизвестном ключе. Соблазн провести
такого бота по текущему алгоритму велик (параметры-то похожи), но это
записало бы в `test_orders` сделки, которых этот алгоритм не совершал,
и отличить их потом было бы нельзя.
"""
import asyncio

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import app.bots.demo_test_bot as m

from app.constants.strategy import LEGACY_ALGORITHM_VERSION, STRATEGY_LEGACY
from app.strategies.base import Algorithm, ConfigError
from app.strategies.registry import (
    UnknownAlgorithm, get_algorithm, known_keys,
)
from tests.strategy_fixtures import LEGACY_STRATEGY_ID
from tests.test_copybot_v3_simulation import (
    FakeCrud, FakeRedis, FakeSessionManager, MARKET, SYMBOL, V3_BOT,
)

# Методы, без которых симулятор не сможет провести ни одной сделки.
CONTRACT = (
    "prepare", "entry_levels", "wait_for_entry", "open_position", "should_exit",
)


def check_registry():
    assert known_keys() == [STRATEGY_LEGACY], known_keys()

    legacy = get_algorithm(STRATEGY_LEGACY)

    assert legacy.key == STRATEGY_LEGACY
    assert legacy.version == LEGACY_ALGORITHM_VERSION, (
        "версия алгоритма попадает в каждую сделку — менять её вместе с "
        "логикой входа/выхода, иначе сделки до и после правки сольются"
    )

    for method in CONTRACT:
        assert callable(getattr(legacy, method, None)), method

    assert isinstance(legacy, Algorithm), (
        "реализация обязана удовлетворять контракту Algorithm"
    )

    print(f"  реестр: {known_keys()}, контракт на месте")


def check_unknown_key():
    for key in (None, "", "strategy_0", "сюрприз"):
        try:
            get_algorithm(key)
        except UnknownAlgorithm as error:
            assert "не реализована" in str(error), error
            continue

        raise AssertionError(f"ключ {key!r} принят, хотя реализации нет")

    print("  незарегистрированный ключ отвергается с понятной ошибкой")


async def check_bot_does_not_trade():
    """Бот незарегистрированной стратегии не открывает сделок."""
    redis = FakeRedis()
    bot = V3_BOT._replace(
        id=777, copybot_v3_time_in_minutes=None,
        copybot_v3_compound_balance=False,
        copy_bot_min_time_profitability_min=None,
        min_timeframe_asset_volatility=None, symbol=SYMBOL,
        strategy_id=LEGACY_STRATEGY_ID,
    )

    stop = asyncio.Event()
    command = m.StartTestBotsCommand(stop_event=stop)
    # Карта стратегий пуста: id бота ни на какой ключ не отображается —
    # так выглядит стратегия, заведённая в базе, но не в коде.
    command._strategy_keys = {}

    provider = AsyncMock()
    provider.get_price.return_value = Decimal("1")
    provider._read_price.return_value = Decimal("1")

    entered = False

    async def wait(*args, **kwargs):
        nonlocal entered
        entered = True
        return "BUY", Decimal("1")

    async def stop_after_sleep(seconds):
        # Первый же уход на новый круг завершает проверку: иначе бот
        # крутился бы здесь до бесконечности.
        stop.set()

    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', FakeCrud), \
            patch.object(m.PriceWatcher, 'wait_for_entry_price', wait), \
            patch.object(m.asyncio, 'sleep', AsyncMock(side_effect=stop_after_sleep)):
        await command.simulate_bot(
            redis, bot, {SYMBOL: dict(MARKET)}, stop, provider, None,
        )

    assert not entered, "бот дошёл до ожидания входа без известного алгоритма"
    assert not redis.pushed, "бот записал сделку без известного алгоритма"

    print("  бот незарегистрированной стратегии не торгует")


def check_config():
    """Настройки проверяет сам алгоритм, опечатки не проходят."""
    legacy = get_algorithm(STRATEGY_LEGACY)

    assert legacy.parse_config(None) is None
    assert legacy.parse_config({}) is None
    assert legacy.parse_config(
        {"schema_version": legacy.config_schema_version}
    ) is None

    for bad in ({"window_seconds": 5}, {"schema_version": 99}):
        try:
            legacy.parse_config(bad)
        except ConfigError:
            continue

        raise AssertionError(f"настройки {bad} приняты, хотя не должны")

    print("  настройки стратегии проверяются, лишние ключи отвергаются")


async def main():
    print("Реестр стратегий:")
    check_config()
    check_registry()
    check_unknown_key()
    await check_bot_does_not_trade()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
