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
    ) -> tuple[TradeType, Decimal]:
        while True:
            current_price = await self.price_provider.get_price(symbol)

            if current_price >= entry_price_buy:
                return TradeType.BUY, current_price
            elif current_price <= entry_price_sell:
                return TradeType.SELL, current_price

            await asyncio.sleep(0.1)
