import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, func, text

from app.crud.asset_history import AssetHistoryCrud
from app.db.base import DatabaseSessionManager
from app.crud.test_bot import TestBotCrud
from app.config import settings
import asyncio

from app.db.models import AssetExchangeSpec, AssetHistory


async def get_average_percentage_for_minimum_tick():
    start_time = time.time()

    average_percent = 0.01

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with (dsm.get_session() as session):
        asset_crud = AssetHistoryCrud(session)
        active_symbols = await asset_crud.get_all_active_pairs()

        if not active_symbols:
            return average_percent

        stmt_active_symbols = (
            select(AssetExchangeSpec.symbol)
            .where(AssetExchangeSpec.symbol.in_(active_symbols))
        )
        result_symbols = await session.execute(stmt_active_symbols)

        actual_active_symbols = {s[0] for s in result_symbols.all()}

        subquery_ranked_prices = (
            select(
                AssetHistory.id,
                AssetHistory.symbol,
                AssetHistory.created_at,
                AssetHistory.last_price,
                func.row_number()
                .over(
                    partition_by=AssetHistory.symbol,
                    order_by=AssetHistory.created_at.desc()
                )
                .label("rn")
            )
            .cte("ranked_prices")
        )

        stmt_latest_prices = (
            select(
                AssetHistory.symbol,
                AssetExchangeSpec.filters,
                subquery_ranked_prices.c.last_price,
            )
            .join(AssetHistory, AssetHistory.id == subquery_ranked_prices.c.id)
            .join(AssetExchangeSpec, AssetExchangeSpec.symbol == AssetHistory.symbol)
            .where(subquery_ranked_prices.c.rn == 1)
            .where(AssetExchangeSpec.symbol.in_(actual_active_symbols))
        )

        result_latest_prices = await session.execute(stmt_latest_prices)
        all_latest_prices_for_active_symbols = result_latest_prices.all()

        percents = []
        symbols_characteristics = {}
        for symbol, filters, last_price in all_latest_prices_for_active_symbols:
            if not filters:
                continue

            tick_size = Decimal(filters[0]['tickSize'])

            percent = (1/(last_price/tick_size)) * 100
            percents.append(percent)

            symbols_characteristics[symbol] = [tick_size, last_price]

        sum_of_percents = sum(percents)
        average_percent = sum_of_percents / len(percents)

        end_time = time.time()
        elapsed_time = end_time - start_time

        minutes = int(elapsed_time // 60)
        seconds = elapsed_time % 60

        print(f"Время, чтобы узнать средний процент по 1 тику: {minutes} минут и {seconds:.2f} секунд")

        print(f'average_percent: {average_percent}')

    return average_percent

async def get_volatile_symbols(session):
    asset_crud = AssetHistoryCrud(session)
    active_symbols = await asset_crud.get_all_active_pairs(is_need_full_info=True)

    if not active_symbols:
        return []

    filtered_symbols = [
        symbol for symbol in active_symbols
        if float(symbol.quote_asset_volume_24h) > 2000000
    ]

    sorted_symbols = sorted(
        filtered_symbols,
        key=lambda x: float(x.price_change_percent_24h),
        reverse=True
    )

    count = len(sorted_symbols)
    top_10_percent_count = int(count * 0.1)
    top_symbols = [
        symbol.symbol for symbol in sorted_symbols[:top_10_percent_count]
    ]

    now = datetime.now(timezone.utc)
    time_ago = now - timedelta(hours=6)

    most_volatiles_h6 = await asset_crud.get_most_volatiles_since_from_symbols_list(
        since=time_ago,
        symbols_list=top_symbols
    )

    time_ago = now - timedelta(hours=3)

    most_volatiles_h3 = await asset_crud.get_most_volatiles_since_from_symbols_list(
        since=time_ago,
        symbols_list=most_volatiles_h6
    )

    most_volatiles_h3_6 = most_volatiles_h3[:6]

    return most_volatiles_h3_6



async def create_bots():
    # average_percent_for_1_tick = await get_average_percentage_for_minimum_tick()

    symbol = "BTCUSDT"

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)

        try:
            sql_command = "TRUNCATE TABLE test_bots RESTART IDENTITY CASCADE;"
            await session.execute(text(sql_command))
        except Exception as e:
            print(e)
            return

        # start_ticks_values = [10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 100]
        # stop_lose_ticks_values = [40, 45, 50, 55, 60, 70, 80, 90, 100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        # stop_win_ticks_values = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        #
        # start_percents_values = [x * average_percent_for_1_tick for x in start_ticks_values]
        # stop_lose_percents_values = [x * average_percent_for_1_tick for x in stop_lose_ticks_values]
        # stop_win_percents_values = [x * average_percent_for_1_tick for x in stop_win_ticks_values]
        #
        # min_tf_volatility_values = [0.5, 1, 2, 3]
        # wait_open_order_values = [0.5]
        #
        # try:
        #     sql_command = "TRUNCATE TABLE test_bots RESTART IDENTITY CASCADE;"
        #     await session.execute(text(sql_command))
        #
        #     for start in start_percents_values:
        #         for stop_lose in stop_lose_percents_values:
        #             new_bots = []
        #             for stop_win in stop_win_percents_values:
        #                 for min_tf in min_tf_volatility_values:
        #                     for wait_min in wait_open_order_values:
        #                         bot_data = {
        #                             "symbol": symbol,
        #                             "balance": Decimal("1000.0"),
        #                             "stop_win_percents": stop_win,
        #                             "stop_loss_percents": stop_lose,
        #                             "start_updown_percents": start,
        #                             "min_timeframe_asset_volatility": min_tf,
        #                             "time_to_wait_for_entry_price_to_open_order_in_minutes": wait_min,
        #                             "is_active": True,
        #                         }
        #                         new_bots.append(bot_data)
        #
        #             await bot_crud.bulk_create(new_bots)
        #             await session.commit()
        #     print(f"✅ Успешно создано ботов. {len(new_bots)}")
        # except Exception as e:
        #     print(e)

        # ma test bots

        symbols = await get_volatile_symbols(session)
        symbols.append('XRPUSDT')

        open_ma_numbers = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100,105,110,115,120,125,130,135,140,145,150]
        close_ma_numbers = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100,105,110,115,120,125,130,135,140,145,150]

        try:
            for symbol in symbols:
                new_bots = []
                for open_ma_number in open_ma_numbers:
                    for close_ma_number in close_ma_numbers:
                        bot_data = {
                            "symbol": symbol,
                            "balance": Decimal("1000.0"),
                            "consider_ma_for_open_order": True,
                            "consider_ma_for_close_order": True,
                            "ma_number_of_candles_for_open_order": open_ma_number,
                            "ma_number_of_candles_for_close_order": close_ma_number,
                            "is_active": True,
                        }
                        new_bots.append(bot_data)

                await bot_crud.bulk_create(new_bots)
                await session.commit()
                print(f"✅ Успешно создано ботов. {len(new_bots)}")
        except Exception as e:
            print(e)

        copy_bot_min_time_profitability_min_values = [10, 20, 30, 40, 50, 60, 120, 180, 240, 360, 420, 480, 540, 600, 660, 720, 1440]

        try:
            new_bots = []
            for min_time in copy_bot_min_time_profitability_min_values:
                bot_data = {
                    "symbol": symbol,
                    "balance": Decimal("1000.0"),
                    "copy_bot_min_time_profitability_min": min_time,
                    "consider_ma_for_open_order": True,
                    "consider_ma_for_close_order": True,
                    "is_active": True,
                }
                new_bots.append(bot_data)

            await bot_crud.bulk_create(new_bots)
            await session.commit()
            print(f"✅ Успешно создано ботов. {len(new_bots)}")
        except Exception as e:
            print(e)


if __name__ == "__main__":
    asyncio.run(create_bots())
