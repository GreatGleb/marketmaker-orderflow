import asyncio
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
        symbol: str | Mapped[str],
        entry_price_buy: Decimal,
        entry_price_sell: Decimal,
        binance_bot,
        bot_config
    ) -> tuple[TradeType, Decimal]:
        while True:
            current_price = await self.price_provider.get_price(symbol)

            if bot_config.consider_ma_for_open_order:
                if bot_config.ma_number_of_candles_for_open_order < bot_config.ma_number_of_candles_for_close_order:
                    less_ma_number = bot_config.ma_number_of_candles_for_open_order
                    more_ma_number = bot_config.ma_number_of_candles_for_close_order
                else:
                    less_ma_number = bot_config.ma_number_of_candles_for_close_order
                    more_ma_number = bot_config.ma_number_of_candles_for_open_order

                less_ma, more_ma = await binance_bot.get_double_ma(symbol=symbol, less_ma_number=less_ma_number, more_ma_number=more_ma_number, current_price=current_price)
                if less_ma and more_ma and current_price > less_ma and current_price > more_ma:
                    return TradeType.BUY.value, current_price
                elif less_ma and more_ma and current_price < less_ma and current_price < more_ma:
                    return TradeType.SELL.value, current_price
            else:
                if current_price >= entry_price_buy:
                    return TradeType.BUY.value, current_price
                elif current_price <= entry_price_sell:
                    return TradeType.SELL.value, current_price

            await asyncio.sleep(0.1)
