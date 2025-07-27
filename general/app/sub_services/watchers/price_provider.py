import asyncio
from decimal import Decimal

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
        symbol: str,
        entry_price_buy: Decimal,
        entry_price_sell: Decimal,
        binance_bot,
        consider_ma_for_open_order: bool
    ) -> tuple[TradeType, Decimal]:
        while True:
            current_price = await self.price_provider.get_price(symbol)

            ma25 = None
            if consider_ma_for_open_order:
                ma25 = await binance_bot.get_ma(symbol=symbol, ma_number=25, current_price=current_price)

            if current_price >= entry_price_buy and (not ma25 or (ma25 and current_price > ma25)):
                return TradeType.BUY.value, current_price
            elif current_price <= entry_price_sell and (not ma25 or (ma25 and current_price < ma25)):
                return TradeType.SELL.value, current_price

            await asyncio.sleep(0.1)
