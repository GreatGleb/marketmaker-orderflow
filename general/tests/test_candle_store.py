"""Формат свечей в Redis: OHLC пишется, старый ключ ещё читается.

Ключ `candles:{SYMBOL}` до перехода хранил список цен закрытия. MA-боты
читают его же, поэтому проверяется оба вида содержимого и склейка
перекрывающихся ответов REST.
"""
import asyncio
import json
from decimal import Decimal

from app.sub_services.watchers.candle_store import (
    candle_from_kline,
    candle_from_ws_kline,
    candles_key,
    closes_from_raw,
    dump_candles,
    load_candles,
    merge_candle,
)


def test_old_format_still_readable():
    raw = json.dumps(["1.5", "1.6", 1.7])

    assert closes_from_raw(raw) == [Decimal("1.5"), Decimal("1.6"), Decimal("1.7")]
    # Размаха у такой свечи нет — она вырождена в точку, а не выдумана.
    assert load_candles(raw)[0] == {"t": None, "o": "1.5", "h": "1.5", "l": "1.5", "c": "1.5"}


def test_new_format_keeps_ohlc():
    kline = [1700000000000, "1.0", "1.4", "0.9", "1.2", "500.0", 1700000059999]
    candle = candle_from_kline(kline)

    assert candle == {"t": 1700000000000, "o": "1.0", "h": "1.4",
                      "l": "0.9", "c": "1.2", "v": "500.0"}
    assert closes_from_raw(dump_candles([candle])) == [Decimal("1.2")]


def test_ws_kline_same_shape():
    data = {"t": 1700000060000, "o": "1.2", "h": "1.3", "l": "1.1", "c": "1.25", "v": "10"}

    assert candle_from_ws_kline(data) == {"t": 1700000060000, "o": "1.2", "h": "1.3",
                                          "l": "1.1", "c": "1.25", "v": "10"}


def test_merge_replaces_same_minute_and_trims():
    # REST отдаёт перекрывающиеся окна: повтор минуты должен заменять,
    # иначе история растёт копиями и MA считается по дублям.
    candles = []
    for minute in range(3):
        candles = merge_candle(candles, candle_from_kline(
            [1700000000000 + minute * 60000, "1", "1", "1", str(minute), "1"]
        ), limit=2)

    candles = merge_candle(candles, candle_from_kline(
        [1700000120000, "1", "1", "1", "99", "1"]
    ), limit=2)

    assert len(candles) == 2
    assert closes_from_raw(dump_candles(candles)) == [Decimal("1"), Decimal("99")]


def test_broken_key_does_not_raise():
    assert load_candles("не json") == []
    assert load_candles(None) == []
    assert load_candles(json.dumps({"c": "1"})) == []
    assert closes_from_raw(json.dumps([{"o": "1", "h": "1", "l": "1", "c": "нечисло"}])) == []


def test_key_name_unchanged():
    assert candles_key("BTCUSDT") == "candles:BTCUSDT"


async def test_ma_reads_new_format():
    """`get_prev_minutes_ma` считает среднюю по свечам, а не по строкам."""
    from unittest.mock import AsyncMock
    from app.bots.binance_bot import BinanceBot

    bot = BinanceBot.__new__(BinanceBot)
    bot.redis = AsyncMock()
    bot.redis.get.return_value = dump_candles([
        candle_from_kline([1700000000000 + i * 60000, "1", "1", "1", str(2 + i), "1"])
        for i in range(4)
    ])

    result = await bot.get_prev_minutes_ma(
        symbol="TESTUSDT", less_ma_number=2, more_ma_number=3,
        minutes=0, current_price=Decimal("6"),
    )

    # closes = 2,3,4,5 + текущая 6. Для «сейчас» функция берёт ma_number+1
    # закрытий (её давнее поведение, здесь оно только фиксируется):
    # MA(2) = (4+5+6)/3, MA(3) = (3+4+5+6)/4.
    assert result["less"]["result"][0] == Decimal("5")
    assert result["more"]["result"][0] == Decimal("4.5")


def main():
    test_old_format_still_readable()
    test_new_format_keeps_ohlc()
    test_ws_kline_same_shape()
    test_merge_replaces_same_minute_and_trims()
    test_broken_key_does_not_raise()
    test_key_name_unchanged()
    asyncio.run(test_ma_reads_new_format())
    print("candle_store: ок")


if __name__ == "__main__":
    main()
