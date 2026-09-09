"""Конфиг копибота: типы полей и неприкосновенность строки test_bots.

Проверяются два конца одной трубы: что `get_bot_config_by_params` кладёт в
Redis (`copy_bot_{id}`) и что из этого собирает
`update_config_from_referral_bot`.

Два свойства, которые легко потерять:

* воркер не пишет в объект, загруженный из базы: autoflush отправит такую
  запись в базу настоящим UPDATE, и NULL в test_bots переживут её только до
  первого commit() в этой сессии;
* в конфиге нет строк вместо чисел — строка '0' истинна для `not`, и на ней
  молча ломаются проверки «поле не задано».
"""
import asyncio
import json
from decimal import Decimal

from app.bots.demo_test_bot import StartTestBotsCommand
from app.constants.volatility import most_volatile_symbol_key
from app.db.models import TestBot
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand as P

# Поля, которые обязаны приехать числом. Строка тут не ошибка формата, а
# работающий бот с неверной логикой.
NUMERIC_FIELDS = (
    "stop_win_percents",
    "stop_loss_percents",
    "start_updown_percents",
    "min_timeframe_asset_volatility",
    "time_to_wait_for_entry_price_to_open_order_in_seconds",
    "ma_number_of_candles_for_open_order",
    "ma_number_of_candles_for_close_order",
    "stop_success_ticks",
    "stop_loss_ticks",
    "start_updown_ticks",
)

# Поля донора, которые в этом сценарии пустые. После сборки конфига они
# обязаны остаться пустыми в самом объекте.
NULL_FIELDS = (
    "stop_success_ticks",
    "stop_loss_ticks",
    "start_updown_ticks",
    "time_to_wait_for_entry_price_to_open_order_in_seconds",
    "ma_number_of_candles_for_open_order",
    "ma_number_of_candles_for_close_order",
)


def make_donor():
    # Так выглядит строка процентного волатильного бота: тиковые уровни
    # пустые, MA не используется, таймаут ожидания входа не задан.
    # 1.20 в min_timeframe_asset_volatility — специально значение, которое не
    # представимо в двоичном float точно.
    return TestBot(
        id=42,
        symbol="BMTUSDT",
        balance=Decimal("1000"),
        stop_success_ticks=None,
        stop_loss_ticks=None,
        start_updown_ticks=None,
        stop_win_percents=Decimal("0.10"),
        stop_loss_percents=Decimal("0.20"),
        start_updown_percents=Decimal("0.05"),
        min_timeframe_asset_volatility=Decimal("1.20"),
        time_to_wait_for_entry_price_to_open_order_in_seconds=None,
        use_trailing_stop=True,
        consider_ma_for_open_order=False,
        consider_ma_for_close_order=False,
        ma_number_of_candles_for_open_order=None,
        ma_number_of_candles_for_close_order=None,
    )


class FakeCrud:
    def __init__(self, donor):
        self.donor = donor

    async def get_bot_by_id(self, bot_id):
        return [self.donor]


class FakeRedis:
    def __init__(self, payload):
        self.payload = payload

    async def get(self, key):
        return self.payload


async def check_donor_not_mutated():
    donor = make_donor()
    config = await P.get_bot_config_by_params(
        bot_crud=FakeCrud(donor), bot_ids=[donor.id]
    )

    assert config, "конфиг донора не собрался"

    for field in NULL_FIELDS:
        assert getattr(donor, field) is None, (
            f"воркер записал {field} в строку test_bots: "
            f"{getattr(donor, field)!r}. Первый же commit() в этой сессии "
            f"заменит NULL на это значение"
        )

    print("  строка test_bots не изменилась: NULL остались NULL")

    return config


def check_types(config):
    # Так конфиг и уезжает в Redis. Decimal сюда положить нельзя — json его не
    # умеет, поэтому дробные поля обязаны быть float.
    restored = json.loads(json.dumps(config))

    assert restored == config, "конфиг не переживает round-trip через JSON"

    for field in NUMERIC_FIELDS:
        value = restored[field]

        assert not isinstance(value, str), (
            f"{field} уехал строкой {value!r}: '0' истинна для not, и "
            f"проверки «поле не задано» у потребителей сломаются"
        )

    for field in ("ma_number_of_candles_for_open_order",
                  "ma_number_of_candles_for_close_order"):
        assert isinstance(restored[field], int), (
            f"{field} — счётчик свечей, ожидается int, "
            f"получено {type(restored[field]).__name__}"
        )

    # Пустой таймаут ожидания входа обязан остаться ложным: и симулятор, и
    # боевой бот на этом откатываются к 1 секунде вместо int('0') = 0.
    assert not restored[
        "time_to_wait_for_entry_price_to_open_order_in_seconds"
    ], "пустой таймаут стал истинным — ожидание входа станет нулевым"

    print("  в конфиге нет строк вместо чисел, JSON round-trip проходит")

    return restored


async def check_copybot_config(payload, donor):
    result = await StartTestBotsCommand.update_config_from_referral_bot(
        bot_config=donor, redis=FakeRedis(json.dumps(payload))
    )
    config = result["config"]

    assert config, "копибот не собрал конфиг из ключа copy_bot_*"

    for field in ("stop_win_percents", "stop_loss_percents",
                  "start_updown_percents", "min_timeframe_asset_volatility",
                  "time_to_wait_for_entry_price_to_open_order_in_seconds"):
        value = getattr(config, field)

        assert isinstance(value, Decimal), (
            f"{field} у копибота {type(value).__name__}, ожидался Decimal: "
            f"арифметика цен идёт на Decimal и упадёт при смешивании с float"
        )

    for field in ("stop_success_ticks", "stop_loss_ticks",
                  "start_updown_ticks", "ma_number_of_candles_for_open_order",
                  "ma_number_of_candles_for_close_order"):
        value = getattr(config, field)

        assert isinstance(value, int), (
            f"{field} у копибота {type(value).__name__}, ожидался int"
        )

    assert config.stop_win_percents == Decimal("0.1"), (
        f"проценты донора приехали с двоичным хвостом: "
        f"{config.stop_win_percents}"
    )

    # Ключ собирается из значения таймфрейма, поэтому хвост от float означал
    # бы ключ, которого воркер set_volatile_pairs никогда не напишет.
    expected_key = most_volatile_symbol_key(
        donor.min_timeframe_asset_volatility
    )
    actual_key = most_volatile_symbol_key(
        config.min_timeframe_asset_volatility
    )

    assert actual_key == expected_key, (
        f"копибот ищет пару по ключу {actual_key}, а воркер пишет "
        f"{expected_key}"
    )

    print(f"  типы у копибота на месте, ключ пары совпадает: {actual_key}")

    return config


async def check_stale_key_format(donor):
    # Ключ copy_bot_*, записанный прошлой версией воркера, живёт в Redis до
    # его следующего цикла — до 30 секунд после обновления.
    stale = {
        "id": 42,
        "symbol": "BMTUSDT",
        "stop_success_ticks": 10,
        "stop_loss_ticks": 10,
        "start_updown_ticks": 5,
        "stop_win_percents": "0.10",
        "stop_loss_percents": "0.20",
        "start_updown_percents": "0.05",
        "min_timeframe_asset_volatility": "1.20",
        "time_to_wait_for_entry_price_to_open_order_in_seconds": "0",
        "use_trailing_stop": True,
        "consider_ma_for_open_order": False,
        "consider_ma_for_close_order": False,
        "ma_number_of_candles_for_open_order": "5.00",
        "ma_number_of_candles_for_close_order": "20.00",
    }

    result = await StartTestBotsCommand.update_config_from_referral_bot(
        bot_config=donor, redis=FakeRedis(json.dumps(stale))
    )
    config = result["config"]

    assert config.ma_number_of_candles_for_open_order == 5
    assert config.ma_number_of_candles_for_close_order == 20
    assert config.stop_win_percents == Decimal("0.10")
    assert most_volatile_symbol_key(
        config.min_timeframe_asset_volatility
    ) == most_volatile_symbol_key(Decimal("1.20"))

    print("  ключ в старом формате (строками) читается без падений")


async def main():
    print("get_bot_config_by_params:")
    config = await check_donor_not_mutated()
    payload = check_types(config)

    print("\nupdate_config_from_referral_bot:")
    await check_copybot_config(payload, make_donor())
    await check_stale_key_format(make_donor())

    print("\nвсё сошлось")


if __name__ == "__main__":
    asyncio.run(main())
