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
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand


async def run():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)

        tf = 60

        time_ago = timedelta(minutes=float(tf))

        profits_data = await bot_crud.get_sorted_by_profit(
            since=time_ago, just_not_copy_bots=True
        )
        filtered_sorted = sorted(
            [item for item in profits_data if item[1] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        ids_tf = [item[0] for item in filtered_sorted]

        time_ago_24h = timedelta(hours=float(24))

        profits_data_24h = await bot_crud.get_sorted_by_profit(
            since=time_ago_24h, just_not_copy_bots=True
        )
        filtered_sorted_24h = sorted(
            [item for item in profits_data_24h if item[1] > 0],
            key=lambda x: x[1],
            reverse=True,
        )

        ids_24h = [item[0] for item in filtered_sorted_24h]
        ids_checked_24h = [item for item in ids_tf if item in ids_24h]

        profits_data_by_referral = await bot_crud.get_sorted_by_profit(
            since=time_ago_24h, just_not_copy_bots=True, by_referral_bot_id=True
        )
        filtered_sorted_by_referral = sorted(
            [item for item in profits_data_by_referral if item[1] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        tf_ids_by_referral = [item[0] for item in filtered_sorted_by_referral]
        ids_checked_by_referral = [item for item in ids_checked_24h if item in tf_ids_by_referral]

        print(f'result: {len(ids_tf)}, result 24h: {len(ids_24h)}, result checked: {len(ids_checked_24h)}, tf_ids_by_referral: {len(tf_ids_by_referral)}, ids_checked_by_referral: {len(ids_checked_by_referral)}')
        for bot_id, total_profit, total_orders, successful_orders in [item for item in filtered_sorted_by_referral if item[0] in ids_checked_24h]:
            print(
                f"Бот {bot_id} — 💰 Общая прибыль: {total_profit:.4f}, "
                f"📈 Успешных ордеров: {successful_orders}/{total_orders}"
            )

    return




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
    asyncio.run(run())
