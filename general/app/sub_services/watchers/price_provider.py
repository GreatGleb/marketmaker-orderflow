import asyncio
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Mapped

from app.enums.trade_type import TradeType


class PriceProvider:
    def __init__(self, redis):
        self.redis = redis

    async def get_price(self, symbol: str) -> Decimal:
        while True:
            try:
                price_str = await self.redis.get(f"price:{symbol}")
                if price_str:
                    return Decimal(price_str)
            except Exception as e:
                print(f"Redis Error: {e}")
            await asyncio.sleep(0.1)


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
