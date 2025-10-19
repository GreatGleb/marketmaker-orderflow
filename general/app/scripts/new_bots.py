import logging
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

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def get_average_percentage_for_minimum_tick():
    start_time = time.time()

    average_percent = 0.01

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with (dsm.get_session() as session):
        asset_crud = AssetHistoryCrud(session)

        active_symbols = await asset_crud.get_all_active_pairs(only_symbols_in_period=True)

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

async def get_most_volatile_symbol():
    logging.info(f'started getting')
    start_time = time.time()

    result = None

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with (dsm.get_session() as session):
        asset_crud = AssetHistoryCrud(session)

        UTC = timezone.utc
        now = datetime.now(UTC)
        days7_ago = now - timedelta(days=7)
        since = days7_ago
        active_symbols = await asset_crud.get_all_active_pairs(since=since, only_symbols_in_period=True)

        print(active_symbols)
        logging.info(f'finished getting')

        if not active_symbols:
            logging.info(f'error not active_symbols')
            return result

        # stmt_active_symbols = (
        #     select(AssetExchangeSpec.symbol)
        #     .where(AssetExchangeSpec.symbol.in_(active_symbols))
        # )
        # result_symbols = await session.execute(stmt_active_symbols)
        # actual_active_symbols = {s[0] for s in result_symbols.all()}
        #
        # print(actual_active_symbols)
        print(len(active_symbols))
        # print(len(actual_active_symbols))
        jumps_sum_by_symbol = {}

        i = 0
        for target_symbol in active_symbols:
            # logging.info(f'started get history_records for {target_symbol}')
            stmt_single_symbol_history = (
                select(
                    AssetHistory.symbol,
                    AssetHistory.created_at,
                    AssetHistory.last_price
                )
                .where(AssetHistory.symbol == target_symbol)
                .order_by(AssetHistory.created_at.asc())
            )

            result = await session.execute(stmt_single_symbol_history)
            history_records = result.all()

            JUMP_THRESHOLD = Decimal('0.5')

            all_candidate_jumps = []
            left = 0

            for right in range(len(history_records)):
                while (history_records[right][1] - history_records[left][1]).total_seconds() > 1.0:
                    left += 1

                window_records = history_records[left: right + 1]
                if not window_records:
                    continue

                prices_in_window = [rec[2] for rec in window_records]
                min_price = min(prices_in_window)
                max_price = max(prices_in_window)

                if min_price > 0:
                    jump = max_price - min_price
                    percentage_jump = (jump / min_price) * 100

                    if percentage_jump > JUMP_THRESHOLD:
                        candidate = {
                            "symbol": target_symbol,
                            "start_time": window_records[0][1],
                            "end_time": window_records[-1][1],
                            "min_price": min_price,
                            "max_price": max_price,
                            "percentage_jump": percentage_jump
                        }
                        all_candidate_jumps.append(candidate)

            if not all_candidate_jumps:
                # logging.info(f"No significant jumps found for {target_symbol}.")
                if i == 0:
                    logging.info(f'history_records 1 item:')
                    logging.info(f'{history_records[0]}')
                    logging.info(f'{history_records[-1]}')
                i += 1
                print(f'got {i} from {len(active_symbols)}')
                continue

            # logging.info(f"Found {len(all_candidate_jumps)} candidate jumps for {target_symbol}. Filtering...")

            sorted_candidates = sorted(all_candidate_jumps, key=lambda x: x['start_time'])

            final_jumps = []
            current_best_event = sorted_candidates[0]

            for j in range(1, len(sorted_candidates)):
                next_jump = sorted_candidates[j]

                if next_jump['start_time'] <= current_best_event['end_time']:
                    if next_jump['percentage_jump'] > current_best_event['percentage_jump']:
                        current_best_event = next_jump
                else:
                    final_jumps.append(current_best_event)
                    current_best_event = next_jump

            final_jumps.append(current_best_event)

            # logging.info(f"Filtered down to {len(final_jumps)} unique jumps for {target_symbol}.")

            for jump in final_jumps:
                symbol = jump['symbol']
                percentage = jump['percentage_jump']

                jumps_sum_by_symbol[symbol] = jumps_sum_by_symbol.get(symbol, Decimal('0')) + percentage

            # logging.info("\n--- Analysis Complete ---")
            if i == 0:
                logging.info(f'history_records 1 item:')
                logging.info(f'{history_records[0]}')
                logging.info(f'{history_records[-1]}')
            # logging.info(f'history_records for {target_symbol}: {len(history_records)}')
            i += 1
            logging.info(f'got {i} from {len(active_symbols)}')

    sorted_jumps_sum = sorted(jumps_sum_by_symbol.items(), key=lambda item: item[1], reverse=True)

    logging.info("\nSorted sums (Array of tuples):")
    for index, (symbol, total_jump) in enumerate(sorted_jumps_sum, start=1):
        log_message = f"{index}. {symbol}: {total_jump:.2f}%"
        print(log_message)

    return result

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

    most_volatiles_h3_6 = most_volatiles_h3[:8]

    return most_volatiles_h3_6

async def deactivate_not_profit_bots(bot_crud):
    bot_symbols = await bot_crud.get_bot_symbols()
    need_to_deactivate_bots = []

    since_timedelta = timedelta(hours=12)

    for symbol in bot_symbols:
        profits_data = await bot_crud.get_sorted_by_profit(
            since=since_timedelta,
            just_not_copy_bots=True,
            symbol=symbol
        )

        sorted_data = sorted(profits_data, key=lambda x: x[1], reverse=True)
        if sorted_data[0][1] < 100:
            need_to_deactivate_bots.append(symbol)
        print(f'Symbol: {symbol}, max profit for 12 hours: {sorted_data[0][1]}')

    print('deactivating bots...')

    for symbol in need_to_deactivate_bots:
        print(f'deactivating {symbol}')
        await bot_crud.deactivate_bot(symbol)

    print(bot_symbols)
    print('bot_symbols')
    print(need_to_deactivate_bots)
    print('need_to_deactivate_bots')


async def create_bots():
    average_percent_for_1_tick = await get_most_volatile_symbol()
    return

    symbol = "ADAUSDT"

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)

        if 1:
            try:
                sql_command = "TRUNCATE TABLE test_bots RESTART IDENTITY CASCADE;"
                await session.execute(text(sql_command))
            except Exception as e:
                print(e)
                return

        if 1:
            start_ticks_values = [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 100]
            stop_lose_ticks_values = [20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 90, 150, 300, 450, 600, 850, 1000]
            stop_win_ticks_values = [5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 150, 300, 450]
            use_trailing_values = [False, True]
            wait_open_order_values = [1, 4]
            #
            # start_percents_values = [x * average_percent_for_1_tick for x in start_ticks_values]
            # stop_lose_percents_values = [x * average_percent_for_1_tick for x in stop_lose_ticks_values]
            # stop_win_percents_values = [x * average_percent_for_1_tick for x in stop_win_ticks_values]

            # myx
            # start_percents_values = [0.005, 0.0075, 0.01, 0.0125, 0.015, 0.0175, 0.02, 0.0225, 0.025, 0.0275, 0.03, 0.035, 0.04, 0.05]
            # stop_lose_percents_values = [0.01, 0.0125, 0.015, 0.0175, 0.02, 0.0225, 0.025, 0.0275, 0.03, 0.035, 0.04, 0.045, 0.075, 0.15, 0.225, 0.3, 0.425, 0.5]
            # stop_win_percents_values = [0.0025, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.075, 0.15, 0.225]

            # ada
            # start_percents_values = [0.01, 0.015, 0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.055, 0.06, 0.07, 0.08,
            #                          0.1]
            # stop_lose_percents_values = [0.02, 0.025, 0.03, 0.035, 0.04, 0.045, 0.05, 0.055, 0.06, 0.07, 0.08, 0.09,
            #                              0.15, 0.3, 0.45, 0.6, 0.85, 1.0]
            # stop_win_percents_values = [0.005, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.1, 0.15, 0.3,
            #                             0.45]

            # min_tf_volatility_values = [0.5, 1, 2, 3]

            try:
                for start in start_ticks_values:
                    for stop_lose in stop_lose_ticks_values:
                        new_bots = []
                        for stop_win in stop_win_ticks_values:
                            # for min_tf in min_tf_volatility_values:
                            for use_traling in use_trailing_values:
                                for wait_min in wait_open_order_values:
                                    bot_data = {
                                        "symbol": symbol,
                                        "balance": Decimal("1000.0"),
                                        "stop_success_ticks": stop_win,
                                        "stop_loss_ticks": stop_lose,
                                        "start_updown_ticks": start,
                                        # "min_timeframe_asset_volatility": min_tf,
                                        "time_to_wait_for_entry_price_to_open_order_in_seconds": wait_min,
                                        "use_trailing_stop": use_traling,
                                        "is_active": True,
                                    }
                                    new_bots.append(bot_data)

                        await bot_crud.bulk_create(new_bots)
                        await session.commit()
                print(f"✅ Успешно создано ботов. {len(new_bots)}")
            except Exception as e:
                print(e)

        if 1:
            copy_bot_min_time_profitability_min_values = [10, 20, 30, 40, 50, 60, 120, 180, 240, 360, 420, 480, 540,
                                                          600, 660, 720, 1440]
            copy_bot_filter_24h_values = [False, True]
            copy_bot_filter_referrals_values = [False, True]

            try:
                new_bots = []
                for min_time in copy_bot_min_time_profitability_min_values:
                    for filter_24h in copy_bot_filter_24h_values:
                        for filter_ref in copy_bot_filter_referrals_values:
                            bot_data = {
                                "symbol": '',
                                "balance": Decimal("1000.0"),
                                "copy_bot_min_time_profitability_min": min_time,
                                "copybot_v1_check_for_24h_profitability": filter_24h,
                                "copybot_v1_check_for_referral_bot_profitability": filter_ref,
                                # "consider_ma_for_open_order": True,
                                # "consider_ma_for_close_order": True,
                            }
                            new_bots.append(bot_data)

                await bot_crud.bulk_create(new_bots)
                await session.commit()
                print(f"✅ Успешно создано ботов. {len(new_bots)}")

                new_bots = []
                for min_time in copy_bot_min_time_profitability_min_values:
                    bot_data = {
                        "symbol": '',
                        "balance": Decimal("1000.0"),
                        "copybot_v2_time_in_minutes": min_time,
                        # "consider_ma_for_open_order": True,
                        # "consider_ma_for_close_order": True,
                    }
                    new_bots.append(bot_data)

                await bot_crud.bulk_create(new_bots)
                await session.commit()
                print(f"✅ Успешно создано ботов. {len(new_bots)}")
            except Exception as e:
                print(e)

        # # ma test bots
        #
        # await deactivate_not_profit_bots(bot_crud)
        #
        # new_symbols = await get_volatile_symbols(session)
        # bot_existing_symbols = await bot_crud.get_bot_symbols()
        # new_symbols = [symbol for symbol in new_symbols if symbol not in bot_existing_symbols]
        # new_symbols = new_symbols[:7 - len(bot_existing_symbols)]
        #
        # print(new_symbols)
        # print('Most volatile symbols')
        #
        # open_ma_numbers = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100,105,110,115,120,125,130,135,140,145,150]
        # close_ma_numbers = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,20,25,30,35,40,45,50,55,60,65,70,75,80,85,90,95,100,105,110,115,120,125,130,135,140,145,150]
        #
        # try:
        #     for symbol in new_symbols:
        #         new_bots = []
        #         for open_ma_number in open_ma_numbers:
        #             for close_ma_number in close_ma_numbers:
        #                 bot_data = {
        #                     "symbol": symbol,
        #                     "balance": Decimal("1000.0"),
        #                     "consider_ma_for_open_order": True,
        #                     "consider_ma_for_close_order": True,
        #                     "ma_number_of_candles_for_open_order": open_ma_number,
        #                     "ma_number_of_candles_for_close_order": close_ma_number,
        #                     "is_active": True,
        #                 }
        #                 new_bots.append(bot_data)
        #
        #         await bot_crud.bulk_create(new_bots)
        #         await session.commit()
        #         print(f"✅ Успешно создано ботов. {len(new_bots)}")
        # except Exception as e:
        #     print(e)

        return


if __name__ == "__main__":
    asyncio.run(create_bots())
