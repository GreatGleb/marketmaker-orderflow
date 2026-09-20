"""Свечи и ATR в горячем цикле: один MGET на процесс, а не GET на бота.

Причина та же, что и у `PriceCache`: цикл удержания будит каждого бота
каждые 100 мс, и поход в Redis из этого места упирается в потолок около
9 500 операций в секунду на процесс. Свеча при этом закрывается раз в
минуту, так что читать чаще, чем раз в несколько секунд, нечего.

ATR считается лениво и запоминается до следующего обновления: период у
ботов свой (его перебирают, как и остальные параметры), а пересчитывать
Уайлдера по шестидесяти свечам на каждом тике каждому боту нельзя.
"""

import asyncio
import logging
import time
from decimal import Decimal

from statistics import median

from app.sub_services.watchers.candle_store import (
    MAX_AGE_SECONDS as CANDLES_MAX_AGE_SECONDS,
    atr,
    atr_percent,
    atr_series,
    candles_key,
    is_stale,
    load_candles,
)


class CandleCache:
    """Снимок `candles:{SYMBOL}` в памяти процесса."""

    REFRESH_INTERVAL_SECONDS = 5.0
    # Пауза после ошибки: долбить упавший Redis каждые 5 с незачем.
    ERROR_RETRY_SECONDS = 15.0
    MAX_AGE_SECONDS = CANDLES_MAX_AGE_SECONDS

    def __init__(self, redis):
        self.redis = redis
        self._candles: dict[str, list[dict]] = {}
        self._atr: dict[tuple[str, int, bool], Decimal | None] = {}
        self._raw: dict[str, str] = {}
        self._symbols: set[str] = set()
        self._task: asyncio.Task | None = None

    def track(self, symbol: str) -> None:
        self._symbols.add(symbol)

    def get(self, symbol: str) -> list[dict]:
        """Свечи пары. Пустой список — свежих свечей сейчас нет."""
        return self._candles.get(symbol, [])

    def get_atr(self, symbol: str, period: int, percent: bool = False):
        """ATR пары. None — истории не хватает либо свечи устарели."""
        key = (symbol, period, percent)

        if key in self._atr:
            return self._atr[key]

        candles = self.get(symbol)
        value = atr_percent(candles, period) if percent else atr(candles, period)
        self._atr[key] = value

        return value

    def get_atr_median(self, symbol: str, period: int):
        """Медиана ATR по всей истории ключа — мерка «обычного» для пары.

        Нужна, чтобы отличить настоящий разгон волатильности от выброса
        на битой свече: сам по себе высокий ATR ни о чём не говорит,
        пока не с чем сравнить.
        """
        key = (symbol, period, "median")

        if key in self._atr:
            return self._atr[key]

        series = atr_series(self.get(symbol), period)
        value = Decimal(str(median(series))) if series else None
        self._atr[key] = value

        return value

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_forever())

    async def _refresh_forever(self) -> None:
        while True:
            try:
                await self._refresh_once()
                delay = self.REFRESH_INTERVAL_SECONDS
            except Exception as error:
                self._candles.clear()
                self._raw.clear()
                self._atr.clear()
                logging.info(f"Кэш свечей: ошибка чтения из Redis: {error}")
                delay = self.ERROR_RETRY_SECONDS

            await asyncio.sleep(delay)

    async def _refresh_once(self) -> None:
        symbols = sorted(self._symbols)

        if not symbols:
            return

        values = await self.redis.mget([candles_key(symbol) for symbol in symbols])
        now_ms = int(time.time() * 1000)
        changed = False

        for symbol, value in zip(symbols, values):
            if value is not None and value == self._raw.get(symbol):
                # Ключ не менялся — разбирать JSON заново незачем, но
                # проверить возраст всё равно надо: питатель мог встать.
                if self._is_stale(self._candles.get(symbol), now_ms):
                    self._candles.pop(symbol, None)
                    self._raw.pop(symbol, None)
                    changed = True
                continue

            candles = load_candles(value)

            if self._is_stale(candles, now_ms):
                candles = []

            if candles:
                self._candles[symbol] = candles
                self._raw[symbol] = value
            else:
                self._candles.pop(symbol, None)
                self._raw.pop(symbol, None)

            changed = True

        if changed:
            # Данные другие — запомненные ATR больше не про них.
            self._atr.clear()

    def _is_stale(self, candles, now_ms: int) -> bool:
        return is_stale(candles, self.MAX_AGE_SECONDS, now_ms)


class CandleProvider:
    """То, что видит стратегия. Без кэша ходит в Redis сама."""

    def __init__(self, redis, cache: "CandleCache | None" = None):
        self.redis = redis
        self.cache = cache

    async def get_candles(self, symbol: str) -> list[dict]:
        if self.cache is not None:
            self.cache.track(symbol)

            return self.cache.get(symbol)

        candles = load_candles(await self.redis.get(candles_key(symbol)))

        # Та же проверка, что и в кэше: путь без кэша не должен отдавать
        # стратегии то, что через кэш она бы не увидела.
        return [] if is_stale(candles) else candles

    async def get_atr(self, symbol: str, period: int) -> Decimal | None:
        if self.cache is not None:
            self.cache.track(symbol)

            return self.cache.get_atr(symbol, period)

        return atr(await self.get_candles(symbol), period)

    async def get_atr_percent(self, symbol: str, period: int) -> Decimal | None:
        """ATR в процентах от цены — в этом виде его и просят стратегии."""
        if self.cache is not None:
            self.cache.track(symbol)

            return self.cache.get_atr(symbol, period, percent=True)

        return atr_percent(await self.get_candles(symbol), period)

    async def get_atr_median(self, symbol: str, period: int) -> Decimal | None:
        """Медиана ATR пары в абсолюте: с чем сравнивать текущий."""
        if self.cache is not None:
            self.cache.track(symbol)

            return self.cache.get_atr_median(symbol, period)

        series = atr_series(await self.get_candles(symbol), period)

        return Decimal(str(median(series))) if series else None
