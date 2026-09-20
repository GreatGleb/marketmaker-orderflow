"""Метрики профиля пары и постраничная загрузка истории.

Метрики решают, на каких парах вообще запускать стратегию прострелов,
поэтому проверяются на свечах с заранее известным ответом, а не на живых.
"""
import asyncio
from decimal import Decimal

from app.sub_services.watchers.candle_history import fetch_history
from app.sub_services.watchers.candle_store import (
    atr_series,
    percentile_of,
    shadow_share,
    shadow_to_body,
    spike_count,
)

MINUTE = 60_000
START = 1_700_000_000_000


def candle(index, open_, high, low, close):
    return {"t": START + index * MINUTE, "o": str(open_), "h": str(high),
            "l": str(low), "c": str(close), "v": "1"}


def test_shadow_to_body_measures_form():
    # Тело 1, тени 3 сверху и 1 снизу: отношение 4.
    assert shadow_to_body(candle(0, 10, 14, 9, 11)) == Decimal(4)
    # Свеча без теней — ноль, как бы велика она ни была.
    assert shadow_to_body(candle(0, 10, 12, 10, 12)) == Decimal(0)
    # Нулевое тело не даёт деления на ноль: отношение упирается в потолок.
    assert shadow_to_body(candle(0, 10, 11, 9, 10)) == Decimal(100)
    # Свеча без размаха — метрики нет.
    assert shadow_to_body(candle(0, 10, 10, 10, 10)) is None


def test_shadow_share_counts_candles_not_averages():
    # Одна свеча-шпилька среди трёх трендовых: доля 25, а не среднее,
    # которое эта шпилька утащила бы вверх.
    candles = [candle(i, 10, 12, 10, 12) for i in range(3)]
    candles.append(candle(3, 10, 14, 9, 11))

    assert shadow_share(candles) == Decimal(25)


def test_spike_counts_only_shadows():
    # Тень вверх на 1% от цены — прострел при пороге 0.5%.
    spike = candle(0, 100, 101, 100, 100)
    # Свеча того же размаха, но ушедшая целиком: это тренд, не прострел.
    trend = candle(1, 100, 101, 100, 101)

    assert spike_count([spike], Decimal("0.5")) == 1
    assert spike_count([trend], Decimal("0.5")) == 0
    assert spike_count([spike], Decimal("2")) == 0


def test_percentile_places_current_atr_in_history():
    quiet = [candle(i, 100, 100.5, 99.5, 100) for i in range(40)]
    loud = [candle(40 + i, 100, 105, 95, 100) for i in range(20)]
    candles = quiet + loud

    series = atr_series(candles, 14)

    assert len(series) == len(candles) - 14
    # Разгон в конце: текущий ATR выше всего, что было раньше.
    assert percentile_of(series, series[-1]) == Decimal(100)
    # Тихая фаза — плато одинаковых значений, и её процентиль равен доле
    # этого плато в ряду, а не нулю: половина истории такая же тихая.
    assert Decimal(50) < percentile_of(series, series[0]) < Decimal(60)
    # Значение ниже всей истории — ноль.
    assert percentile_of(series, Decimal(0)) == Decimal(0)


def test_atr_series_matches_step_by_step():
    from app.sub_services.watchers.candle_store import atr

    candles = [candle(i, 100, 100 + i % 4, 99 - i % 3, 100 + i % 2) for i in range(40)]
    series = atr_series(candles, 14)

    # Инкрементальный ряд обязан совпасть с прямым расчётом на префиксах.
    assert series[0] == atr(candles[:15], 14)
    assert series[5] == atr(candles[:20], 14)
    assert series[-1] == atr(candles, 14)


class FakeClient:
    """Отдаёт страницы по 1000 свечей, как Binance."""

    def __init__(self, total_minutes):
        self.total = total_minutes
        self.requests = []

    async def get(self, url, params):
        self.requests.append(params)
        end = params.get("endTime")
        limit = params["limit"]
        newest = self.total - 1 if end is None else (end - START) // MINUTE

        indexes = [i for i in range(max(0, newest - limit + 1), newest + 1)]
        klines = [[START + i * MINUTE, "100", "101", "99", "100", "1"] for i in indexes]

        return FakeResponse(klines)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


async def test_history_pages_backwards():
    client = FakeClient(total_minutes=2500)

    candles = await fetch_history(client, "TESTUSDT", hours=40)

    # 40 часов — это 2400 закрытых свечей: три страницы, из которых
    # последняя, ещё не закрытая, отброшена.
    assert len(client.requests) == 3
    assert len(candles) == 2400
    # Порядок по возрастанию времени и без дублей.
    times = [item["t"] for item in candles]
    assert times == sorted(times) == sorted(set(times))


async def test_history_stops_when_exchange_runs_out():
    # Пара торгуется 100 минут, а просят 40 часов: лишних кругов быть не должно.
    client = FakeClient(total_minutes=100)

    candles = await fetch_history(client, "NEWUSDT", hours=40)

    assert len(candles) == 99
    assert len(client.requests) == 1


def test_atr_threshold_is_comparable_between_pairs():
    """Порог в ATR приводит пары к общей мерке, порог в процентах — нет."""
    from app.scripts.pair_profile_report import profile

    # Одна пара спокойная, вторая ходит в десять раз шире. Выносы у обеих
    # одинаковы относительно собственной волатильности.
    quiet = [candle(i, 100, 100.1, 99.9, 100) for i in range(60)]
    loud = [candle(i, 100, 101, 99, 100) for i in range(60)]

    for candles, shadow in ((quiet, "100.5"), (loud, "105")):
        candles[30] = candle(30, 100, shadow, 99.9 if candles is quiet else 99, 100)

    quiet_row = profile("QUIET", quiet, 14, Decimal("0.5"), Decimal(2))
    loud_row = profile("LOUD", loud, 14, Decimal("0.5"), Decimal(2))

    # Пороги разные, потому что считаются от ATR каждой пары.
    assert quiet_row["threshold"] < loud_row["threshold"]
    # А результат сравним: вынос засчитан обеим.
    assert quiet_row["spikes"] == loud_row["spikes"] > 0

    # С общим порогом в процентах шумная пара выглядит прострельнее на
    # порядки — хотя относительно себя обе ведут себя одинаково. Именно
    # поэтому колонка и считается в ATR.
    quiet_fixed = profile("QUIET", quiet, 14, Decimal("0.5"), None)
    loud_fixed = profile("LOUD", loud, 14, Decimal("0.5"), None)
    assert loud_fixed["spikes"] > quiet_fixed["spikes"] * 10


def main():
    test_shadow_to_body_measures_form()
    test_shadow_share_counts_candles_not_averages()
    test_spike_counts_only_shadows()
    test_percentile_places_current_atr_in_history()
    test_atr_series_matches_step_by_step()
    test_atr_threshold_is_comparable_between_pairs()
    asyncio.run(test_history_pages_backwards())
    asyncio.run(test_history_stops_when_exchange_runs_out())
    print("pair_profile: ок")


if __name__ == "__main__":
    main()
