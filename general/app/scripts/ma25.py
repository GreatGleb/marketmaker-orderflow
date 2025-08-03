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

from app.dependencies import redis_context


async def ma():
    async with redis_context() as redis:
        binance_bot = BinanceBot(is_need_prod_for_data=True, redis=redis)

        symbol = 'XRPUSDT'
        m = await binance_bot.get_ma(symbol, 25)
        double = await binance_bot.get_double_ma(symbol=symbol, less_ma_number=10, more_ma_number=25)
        history = await binance_bot.get_prev_minutes_ma(symbol=symbol, less_ma_number=10, more_ma_number=25, minutes=5)

        print(f'm: {m}')
        print(f'double: {double}')
        print(f'history: {history}')








if __name__ == "__main__":
    asyncio.run(ma())
