import time
from datetime import timezone, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, func, text

from app.bots.binance_bot import BinanceBot
from app.crud.asset_history import AssetHistoryCrud
from app.db.base import DatabaseSessionManager
from app.crud.test_bot import TestBotCrud
from app.config import settings
import asyncio

from app.db.models import AssetExchangeSpec, AssetHistory

from binance.client import Client

from app.dependencies import redis_context
from app.sub_services.watchers.price_provider import PriceProvider


async def ma():
    binance_bot = BinanceBot()

    symbol = 'BTCUSDT'
    m = await binance_bot.get_ma(symbol, 25)

    print(f'm: {m}')








if __name__ == "__main__":
    asyncio.run(ma())
