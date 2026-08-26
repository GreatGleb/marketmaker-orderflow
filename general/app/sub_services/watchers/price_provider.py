import asyncio
import logging
import time
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped

from app.enums.trade_type import TradeType


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

    def __init__(self, redis):
        self.redis = redis

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

    async def get_price(self, symbol: str) -> Decimal:
        # Ждём цену бесконечно: бросать открытую позицию из-за паузы в
        # питателе цен нельзя. Но молчать об этом тоже нельзя — иначе бот
        # выглядит работающим, а на деле стоит.
        attempt = 0
        started_at = time.monotonic()

        while True:
            try:
                price_str = await self.redis.get(f"price:{symbol}")
                if price_str:
                    if attempt:
                        self._last_missing_log.pop(symbol, None)
                    return Decimal(price_str)
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
    def __init__(self, redis):
        self.redis = redis
        self.price_provider = PriceProvider(redis)

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
