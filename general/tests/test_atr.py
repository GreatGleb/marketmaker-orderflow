"""ATR по свечам из Redis и кэш, из которого его берёт горячий цикл.

Эталон считается здесь же прямым перебором по определению Уайлдера:
сверять реализацию с ней самой смысла нет.
"""
import asyncio
import json
from decimal import Decimal

from app.sub_services.watchers.candle_provider import CandleCache, CandleProvider
from app.sub_services.watchers.candle_store import (
    atr,
    atr_percent,
    candle_from_kline,
    candles_key,
    dump_candles,
    true_range,
)

MINUTE = 60_000
START = 1_700_000_000_000


def candle(index, high, low, close, open_=None):
    return candle_from_kline([
        START + index * MINUTE, str(open_ if open_ is not None else close),
        str(high), str(low), str(close), "1",
    ])


def reference_atr(candles, period):
    """Определение Уайлдера в лоб: seed — среднее, дальше сглаживание."""
    ranges = []

    for index in range(1, len(candles)):
        high = Decimal(candles[index]["h"])
        low = Decimal(candles[index]["l"])
        previous = Decimal(candles[index - 1]["c"])
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))

    value = sum(ranges[:period]) / Decimal(period)

    for item in ranges[period:]:
        value = (value * (period - 1) + item) / Decimal(period)

    return value


def test_true_range_counts_gap():
    # Свеча целиком выше прошлого закрытия: разрыв входит в диапазон.
    assert true_range(candle(1, 12, 11, 11.5), Decimal("10")) == Decimal("2")
    # Без прошлого закрытия остаётся только размах самой свечи.
    assert true_range(candle(1, 12, 11, 11.5), None) == Decimal("1")


def test_atr_matches_definition():
    candles = [candle(i, 10 + i % 3, 9 - i % 2, 9.5 + i % 2) for i in range(30)]

    assert atr(candles, 14) == reference_atr(candles, 14)


def test_atr_needs_enough_history():
    candles = [candle(i, 10, 9, 9.5) for i in range(14)]

    # 14 свечей дают 13 полных диапазонов — для ATR(14) этого мало.
    assert atr(candles, 14) is None
    assert atr(candles + [candle(14, 10, 9, 9.5)], 14) == Decimal("1")


def test_atr_percent_relative_to_last_close():
    candles = [candle(i, 101, 99, 100) for i in range(20)]

    assert atr(candles, 14) == Decimal("2")
    assert atr_percent(candles, 14) == Decimal("2")


def test_broken_candle_does_not_fake_result():
    candles = [candle(i, 10, 9, 9.5) for i in range(20)]
    candles[5] = {"t": candles[5]["t"], "o": "1", "h": "нечисло", "l": "9", "c": "9.5"}

    assert atr(candles, 14) is None


class FakeRedis:
    def __init__(self, values):
        self.values = values
        self.mget_calls = 0

    async def mget(self, keys):
        self.mget_calls += 1

        return [self.values.get(key) for key in keys]

    async def get(self, key):
        return self.values.get(key)


async def test_cache_reads_once_for_all_bots():
    import time

    now_ms = int(time.time() * 1000)
    candles = [
        candle_from_kline([now_ms - (20 - i) * MINUTE, "100", "101", "99", "100", "1"])
        for i in range(20)
    ]
    redis = FakeRedis({candles_key("BMTUSDT"): dump_candles(candles)})

    cache = CandleCache(redis)
    provider = CandleProvider(redis, cache=cache)

    # Первое обращение только регистрирует пару: данных ещё нет.
    assert await provider.get_atr("BMTUSDT", 14) is None
    await cache._refresh_once()

    # Тысяча ботов — ни одного нового запроса в Redis и один расчёт ATR.
    for _ in range(1000):
        assert await provider.get_atr("BMTUSDT", 14) == Decimal("2")

    assert redis.mget_calls == 1

    # Повторное чтение того же содержимого не сбрасывает посчитанное.
    await cache._refresh_once()
    assert redis.mget_calls == 2
    assert cache._atr


async def test_stale_candles_are_not_served():
    old = [
        candle_from_kline([START + i * MINUTE, "100", "101", "99", "100", "1"])
        for i in range(20)
    ]
    redis = FakeRedis({candles_key("OLDUSDT"): dump_candles(old)})

    cache = CandleCache(redis)
    cache.track("OLDUSDT")
    await cache._refresh_once()

    # Питатель свечей встал — лучше без ATR, чем ATR позапрошлогодний.
    assert cache.get("OLDUSDT") == []
    assert cache.get_atr("OLDUSDT", 14) is None


async def test_provider_without_cache_reads_redis():
    import time

    now_ms = int(time.time() * 1000)
    candles = [
        candle_from_kline([now_ms - (20 - i) * MINUTE, "100", "101", "99", "100", "1"])
        for i in range(20)
    ]
    redis = FakeRedis({candles_key("BMTUSDT"): json.dumps(candles)})

    provider = CandleProvider(redis)

    assert await provider.get_atr_percent("BMTUSDT", 14) == Decimal("2")


def main():
    test_true_range_counts_gap()
    test_atr_matches_definition()
    test_atr_needs_enough_history()
    test_atr_percent_relative_to_last_close()
    test_broken_candle_does_not_fake_result()
    asyncio.run(test_cache_reads_once_for_all_bots())
    asyncio.run(test_stale_candles_are_not_served())
    asyncio.run(test_provider_without_cache_reads_redis())
    print("atr: ок")


if __name__ == "__main__":
    main()
