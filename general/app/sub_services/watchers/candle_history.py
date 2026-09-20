"""История минутных свечей по REST — для отчётов, не для горячего цикла.

Redis держит последний час (`MIN_HISTORY`), и на нём не посчитать, высок
ли сейчас ATR относительно обычного для этой пары: сравнивать не с чем.
Процентиль требует истории за несколько суток, и тянуть её удобнее в
момент отчёта, чем копить фоном — отчёт тогда не зависит от того, что
процесс работал всё это время.

По умолчанию рынок — фьючерсы: по фьючерсным ценам работает симулятор,
и метрики должны считаться по тем же данным. REST `fapi` с текущего IP
не ограничен, в отличие от фьючерсного WS.

Одна страница Binance — 1000 свечей, то есть примерно 16 часов.
"""

import asyncio
import logging

from app.sub_services.watchers.candle_store import candle_from_kline

FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
SPOT_KLINES_URL = "https://api.binance.com/api/v3/klines"
PAGE_LIMIT = 1000
MINUTE_MS = 60_000


def klines_url(market: str) -> str:
    return SPOT_KLINES_URL if market == "spot" else FUTURES_KLINES_URL


async def fetch_history(client, symbol: str, hours: float, interval: str = "1m",
                        market: str = "futures") -> list[dict]:
    """Минутные свечи за последние `hours` часов, по возрастанию времени.

    Страницы берутся от конца к началу: последняя свеча нужна всегда, а
    глубина упирается в то, сколько пара вообще торгуется.
    """
    wanted = int(hours * 60)
    collected: dict[int, dict] = {}
    end_time = None

    while len(collected) < wanted:
        params = {"symbol": symbol, "interval": interval,
                  "limit": min(PAGE_LIMIT, wanted - len(collected) + 1)}

        if end_time is not None:
            params["endTime"] = end_time

        response = await client.get(klines_url(market), params=params)
        response.raise_for_status()
        klines = response.json()

        if not isinstance(klines, list) or not klines:
            break

        for kline in klines:
            candle = candle_from_kline(kline)
            collected[candle["t"]] = candle

        oldest = min(int(kline[0]) for kline in klines)

        if end_time is not None and oldest >= end_time:
            # Биржа отдала ту же страницу — история кончилась.
            break

        end_time = oldest - 1

        if len(klines) < params["limit"]:
            break

    candles = [collected[key] for key in sorted(collected)]

    # Последняя свеча ещё открыта: её high/low не окончательные.
    return candles[:-1] if candles else []


async def fetch_many(client, symbols: list[str], hours: float, concurrency: int = 4,
                     market: str = "futures") -> dict[str, list[dict]]:
    """История по нескольким парам. Пара без такого рынка пропускается."""
    semaphore = asyncio.Semaphore(concurrency)

    async def one(symbol: str):
        async with semaphore:
            try:
                return symbol, await fetch_history(client, symbol, hours, market=market)
            except Exception as error:
                logging.info(f"{symbol}: история не получена ({error}).")

                return symbol, []

    results = await asyncio.gather(*[one(symbol) for symbol in symbols])

    return {symbol: candles for symbol, candles in results if candles}
