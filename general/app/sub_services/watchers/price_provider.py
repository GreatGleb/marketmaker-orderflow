import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped

from app.enums.trade_type import TradeType


class PriceCache:
    """Один MGET на все нужные пары вместо GET на каждого бота.

    Раньше каждый бот ходил в Redis каждые 0.1 с. Потолок одного процесса
    Python — около 9 500 операций в секунду независимо от числа ботов, и на
    2 000 ботов такт цикла удержания растягивался со 100 до 195 мс, а на
    17 000 — до 1 671 мс. С кэшем в Redis уходит один MGET раз в 50 мс, а
    боты читают из словаря вообще без ввода-вывода.

    Кэш держит только те пары, которые реально спрашивают: волатильный режим
    выбирает пару на ходу, заранее список не известен.
    """

    REFRESH_INTERVAL_SECONDS = 0.05
    # Пауза после ошибки: долбить упавший Redis каждые 50 мс незачем.
    ERROR_RETRY_SECONDS = 1.0

    def __init__(self, redis):
        self.redis = redis
        self._prices: dict[str, Decimal] = {}
        self._symbols: set[str] = set()
        self._task: asyncio.Task | None = None

    def track(self, symbol: str) -> None:
        """Добавляет пару в список обновляемых."""
        self._symbols.add(symbol)

    def get(self, symbol: str) -> Decimal | None:
        """Последняя известная цена. None — цены сейчас нет."""
        return self._prices.get(symbol)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._refresh_forever())

    async def _refresh_forever(self) -> None:
        while True:
            try:
                await self._refresh_once()
                delay = self.REFRESH_INTERVAL_SECONDS
            except Exception as e:
                logging.info(f"Кэш цен: ошибка чтения из Redis: {e}")
                delay = self.ERROR_RETRY_SECONDS

            await asyncio.sleep(delay)

    async def _refresh_once(self) -> None:
        symbols = sorted(self._symbols)

        if not symbols:
            return

        values = await self.redis.mget([f"price:{s}" for s in symbols])

        for symbol, value in zip(symbols, values):
            if value is None:
                # Ключ протух (TTL 120 с в watch_ws_and_save) или пара выпала
                # из watched_pair. Убираем из кэша, чтобы боты ждали, а не
                # торговали по замороженной цене.
                self._prices.pop(symbol, None)
            else:
                self._prices[symbol] = Decimal(value)


class PriceProvider:
    # Сколько попыток опрашивать часто, прежде чем перейти на редкий опрос.
    FAST_POLL_ATTEMPTS = 50
    FAST_POLL_INTERVAL_SECONDS = 0.1
    SLOW_POLL_INTERVAL_SECONDS = 1.0
    # Как часто напоминать в лог, что цены по паре всё ещё нет.
    MISSING_LOG_INTERVAL_SECONDS = 60

    # Общий на процесс: тысячи ботов на одной паре не должны писать
    # тысячи одинаковых строк в лог.
    _last_missing_log: dict[str, float] = {}

    def __init__(self, redis, cache: "PriceCache | None" = None):
        self.redis = redis
        # Без кэша ходим в Redis напрямую — так работает боевой binance_bot.
        self.cache = cache

    @classmethod
    def _log_missing_price(cls, symbol: str, waiting_seconds: float) -> None:
        now = time.monotonic()
        last = cls._last_missing_log.get(symbol)

        if last is not None and now - last < cls.MISSING_LOG_INTERVAL_SECONDS:
            return

        cls._last_missing_log[symbol] = now
        logging.info(
            f"⏳ Нет цены в Redis по ключу price:{symbol} "
            f"уже {waiting_seconds:.0f} с — боты на этой паре стоят. "
            f"Проверьте app.scripts.watch_ws_and_save."
        )

    async def _read_price(self, symbol: str) -> Decimal | None:
        """Одна попытка получить цену. None — цены сейчас нет."""
        if self.cache is not None:
            self.cache.track(symbol)

            return self.cache.get(symbol)

        price_str = await self.redis.get(f"price:{symbol}")

        return Decimal(price_str) if price_str else None

    async def get_price(self, symbol: str) -> Decimal:
        # Ждём цену бесконечно: бросать открытую позицию из-за паузы в
        # питателе цен нельзя. Но молчать об этом тоже нельзя — иначе бот
        # выглядит работающим, а на деле стоит.
        attempt = 0
        started_at = time.monotonic()

        while True:
            try:
                price = await self._read_price(symbol)
                if price is not None:
                    if attempt:
                        self._last_missing_log.pop(symbol, None)
                    return price
            except Exception as e:
                logging.info(f"Redis Error: {e}")

            attempt += 1

            # Первые секунды молчим: цена может появиться с небольшой
            # задержкой, и ругаться на это бессмысленно. Жалуемся, только
            # когда пауза перестала быть мгновенной.
            if attempt >= self.FAST_POLL_ATTEMPTS:
                self._log_missing_price(symbol, time.monotonic() - started_at)

            # Первые попытки — часто: цена обычно появляется сразу. Дальше
            # реже, иначе тысячи ботов на мёртвой паре забьют Redis
            # бессмысленными запросами.
            await asyncio.sleep(
                self.FAST_POLL_INTERVAL_SECONDS
                if attempt < self.FAST_POLL_ATTEMPTS
                else self.SLOW_POLL_INTERVAL_SECONDS
            )


class PriceWatcher:
    def __init__(self, redis, price_provider: PriceProvider | None = None):
        self.redis = redis
        # Свой провайдер — только если готового не дали: иначе потеряется
        # общий кэш цен, ради которого всё и затевалось.
        self.price_provider = price_provider or PriceProvider(redis)

    async def wait_for_entry_price(
        self,
        binance_bot,
        bot_config,
        symbol: str | Mapped[str],
        entry_price_buy: Decimal | None = None,
        entry_price_sell: Decimal | None = None,
    ) -> tuple[TradeType, Decimal]:
        while True:
            current_price = await self.price_provider.get_price(symbol)

            if bot_config.consider_ma_for_open_order:
                if int(bot_config.ma_number_of_candles_for_open_order) < int(bot_config.ma_number_of_candles_for_close_order):
                    less_ma_number = int(bot_config.ma_number_of_candles_for_open_order)
                    more_ma_number = int(bot_config.ma_number_of_candles_for_close_order)
                else:
                    less_ma_number = int(bot_config.ma_number_of_candles_for_close_order)
                    more_ma_number = int(bot_config.ma_number_of_candles_for_open_order)

                ma_data = await binance_bot.get_prev_minutes_ma(
                    symbol=symbol,
                    less_ma_number=less_ma_number,
                    more_ma_number=more_ma_number,
                    minutes=2,
                    current_price=current_price
                )

                if not ma_data:
                    print("Недостаточно данных для принятия решения.")
                    await asyncio.sleep(10)
                    continue

                less_ma_history = ma_data['less']['result']
                more_ma_history = ma_data['more']['result']

                if len(less_ma_history) < (2+1) or len(more_ma_history) < (2+1):
                    print("Недостаточно истории MA для проверки пересечения.")
                    await asyncio.sleep(10)
                    continue

                less_ma_current = less_ma_history[0]
                more_ma_current = more_ma_history[0]

                is_it_buy = False
                is_it_sell = False

                for minute in range(1, (2 + 1)):
                    less_ma_prev = less_ma_history[minute]
                    more_ma_prev = more_ma_history[minute]

                    if None in [less_ma_prev, less_ma_current, more_ma_prev, more_ma_current]:
                        continue

                    # Золотой крест: быстрая MA пересекла медленную снизу вверх
                    if less_ma_prev < more_ma_prev and less_ma_current > more_ma_current:
                        # print(
                        #     f"Сигнал на покупку: Золотой крест. Fast MA ({less_ma_number}) пересекла Slow MA ({more_ma_number}) снизу вверх. на {symbol} в "
                        # )
                        # print(datetime.now().strftime("%H:%M:%S"))
                        is_it_buy = True

                    # Крест смерти: быстрая MA пересекла медленную сверху вниз
                    elif less_ma_prev > more_ma_prev and less_ma_current < more_ma_current:
                        # print(
                        #     f"Сигнал на продажу: Крест смерти. Fast MA ({less_ma_number}) пересекла Slow MA ({more_ma_number}) сверху вниз. на {symbol} в "
                        # )
                        # print(datetime.now().strftime("%H:%M:%S"))
                        is_it_sell = True

                if is_it_buy:
                    return TradeType.BUY.value, current_price
                elif is_it_sell:
                    return TradeType.SELL.value, current_price
            else:
                if current_price >= entry_price_buy:
                    return TradeType.BUY.value, current_price
                elif current_price <= entry_price_sell:
                    return TradeType.SELL.value, current_price

            await asyncio.sleep(0.1)
