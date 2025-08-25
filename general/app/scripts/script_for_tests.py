import time
from datetime import timezone, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, func, text

from app.bots.binance_bot import BinanceBot
from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.db.base import DatabaseSessionManager
from app.crud.test_bot import TestBotCrud
from app.config import settings
import asyncio

from app.dependencies import redis_context
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand


async def select_volatile_pair():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        data = []
        exchange_crud = AssetExchangeSpecCrud(session)
        symbols = await exchange_crud.get_all_symbols()
        symbols = [symbol[0] for symbol in symbols]

        binance_bot = BinanceBot(is_need_prod_for_data=True)

        for symbol in symbols:
            fees = await binance_bot.fetch_fees_data(symbol)
            klines = await binance_bot.get_monthly_klines(symbol=symbol)
            data.append(fees)
            await asyncio.sleep(1)
            break

        print(len(symbols))
        print(klines)
        print(data)


async def run():
    await select_volatile_pair()

    return 0
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        shared_data = {}
        asset_crud = AssetHistoryCrud(session)
        exchange_crud = AssetExchangeSpecCrud(session)
        symbols = await asset_crud.get_all_active_pairs()

        for symbol in symbols:
            step_sizes = await exchange_crud.get_step_size_by_symbol(
                symbol
            )
            shared_data[symbol] = {
                "tick_size": (
                    Decimal(str(step_sizes.get("tick_size")))
                    if step_sizes
                    else None
                )
            }

    return 0
    #
    #     time_ago = timedelta(minutes=float(tf))
    #
    #     profits_data = await bot_crud.get_sorted_by_profit(
    #         since=time_ago, just_not_copy_bots=True
    #     )
    #     filtered_sorted = sorted(
    #         [item for item in profits_data if item[1] > 0],
    #         key=lambda x: x[1],
    #         reverse=True,
    #     )
    #     ids_tf = [item[0] for item in filtered_sorted]
    #
    #     time_ago_24h = timedelta(hours=float(24))
    #
    #     profits_data_24h = await bot_crud.get_sorted_by_profit(
    #         since=time_ago_24h, just_not_copy_bots=True
    #     )
    #     filtered_sorted_24h = sorted(
    #         [item for item in profits_data_24h if item[1] > 0],
    #         key=lambda x: x[1],
    #         reverse=True,
    #     )
    #
    #     ids_24h = [item[0] for item in filtered_sorted_24h]
    #     ids_checked_24h = [item for item in ids_tf if item in ids_24h]
    #
    #     profits_data_by_referral = await bot_crud.get_sorted_by_profit(
    #         since=time_ago_24h, just_not_copy_bots=True, by_referral_bot_id=True
    #     )
    #     filtered_sorted_by_referral = sorted(
    #         [item for item in profits_data_by_referral if item[1] > 0],
    #         key=lambda x: x[1],
    #         reverse=True,
    #     )
    #     tf_ids_by_referral = [item[0] for item in filtered_sorted_by_referral]
    #     ids_checked_by_referral = [item for item in ids_checked_24h if item in tf_ids_by_referral]
    #
    #     print(f'result: {len(ids_tf)}, result 24h: {len(ids_24h)}, result checked: {len(ids_checked_24h)}, tf_ids_by_referral: {len(tf_ids_by_referral)}, ids_checked_by_referral: {len(ids_checked_by_referral)}')
    #     for bot_id, total_profit, total_orders, successful_orders in [item for item in filtered_sorted_by_referral if item[0] in ids_checked_24h]:
    #         print(
    #             f"Бот {bot_id} — 💰 Общая прибыль: {total_profit:.4f}, "
    #             f"📈 Успешных ордеров: {successful_orders}/{total_orders}"
    #         )
    #     print(ids_checked_by_referral)
    #     for bot_id, total_profit, total_orders, successful_orders in [item for item in filtered_sorted if item[0] in ids_24h and item[0] in tf_ids_by_referral]:
    #         print(
    #             f"Бот {bot_id} — 💰 Общая прибыль: {total_profit:.4f}, "
    #             f"📈 Успешных ордеров: {successful_orders}/{total_orders}"
    #         )
    #
    # return
    #
    #
    #
    #
    # async with redis_context() as redis:
    #     binance_bot = BinanceBot(is_need_prod_for_data=True, redis=redis)
    #
    #     symbol = 'XRPUSDT'
    #     m = await binance_bot.get_ma(symbol, 25)
    #     double = await binance_bot.get_double_ma(symbol=symbol, less_ma_number=10, more_ma_number=25)
    #     history = await binance_bot.get_prev_minutes_ma(symbol=symbol, less_ma_number=10, more_ma_number=25, minutes=5)
    #
    #     print(f'm: {m}')
    #     print(f'double: {double}')
    #     print(f'history: {history}')





if __name__ == "__main__":
    asyncio.run(run())
