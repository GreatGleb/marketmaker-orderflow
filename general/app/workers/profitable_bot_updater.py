import asyncio
import json

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.crud.test_bot import TestBotCrud
from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)

from app.utils import Command


class ProfitableBotUpdaterCommand(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    @staticmethod
    async def get_copybot_config(
            bot_crud, copybot_v2_time_in_minutes = 60
    ):
        copy_bot = None
        copy_bot_id = None

        copybot_v2_time_in_minutes = int(copybot_v2_time_in_minutes)

        try:
            profits_data = await bot_crud.get_sorted_by_profit(since=timedelta(minutes=copybot_v2_time_in_minutes), just_copy_bots=True)
            profits_data_filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)

            try:
                copy_bot_id = profits_data_filtered_sorted[0][0]
            except (IndexError, TypeError):
                pass

            if copy_bot_id:
                copy_bots = await bot_crud.get_bot_by_id(
                    bot_id=copy_bot_id
                )
                if copy_bots:
                    copy_bot = copy_bots[0]
        except Exception as e:
            print('Failed to get copybot data')
            print(e)

        return copy_bot

    @staticmethod
    async def get_bot_config_by_params(
        bot_crud, tf_bot_ids, copy_bot_min_time_profitability_min
    ):
        min_bot_ids = tf_bot_ids[copy_bot_min_time_profitability_min]

        print(min_bot_ids)

        if min_bot_ids:
            refer_bot = await bot_crud.get_bot_by_id(
                bot_id=min_bot_ids[0]
            )

            if refer_bot:
                refer_bot = refer_bot[0]

                if refer_bot.stop_win_percents is None:
                    refer_bot.stop_win_percents = 0
                if refer_bot.stop_loss_percents is None:
                    refer_bot.stop_loss_percents = 0
                if refer_bot.start_updown_percents is None:
                    refer_bot.start_updown_percents = 0
                if refer_bot.min_timeframe_asset_volatility is None:
                    refer_bot.min_timeframe_asset_volatility = 0
                if refer_bot.min_timeframe_asset_volatility is None:
                    refer_bot.min_timeframe_asset_volatility = 0
                if refer_bot.ma_number_of_candles_for_open_order is None:
                    refer_bot.ma_number_of_candles_for_open_order = 0
                if refer_bot.ma_number_of_candles_for_close_order is None:
                    refer_bot.ma_number_of_candles_for_close_order = 0

                refer_bot_dict = {
                    "id": refer_bot.id,
                    "symbol": refer_bot.symbol,
                    "stop_success_ticks": refer_bot.stop_success_ticks,
                    "stop_loss_ticks": refer_bot.stop_loss_ticks,
                    "start_updown_ticks": refer_bot.start_updown_ticks,
                    "stop_win_percents": str(refer_bot.stop_win_percents),
                    "stop_loss_percents": str(refer_bot.stop_loss_percents),
                    "start_updown_percents": str(refer_bot.start_updown_percents),
                    "min_timeframe_asset_volatility": str(
                        refer_bot.min_timeframe_asset_volatility
                    ),
                    "time_to_wait_for_entry_price_to_open_order_in_minutes": str(
                        refer_bot.time_to_wait_for_entry_price_to_open_order_in_minutes
                        or 0
                    ),
                    "consider_ma_for_open_order": refer_bot.consider_ma_for_open_order,
                    "consider_ma_for_close_order": refer_bot.consider_ma_for_close_order,
                    "ma_number_of_candles_for_open_order": str(
                        refer_bot.ma_number_of_candles_for_open_order
                    ),
                    "ma_number_of_candles_for_close_order": str(
                        refer_bot.ma_number_of_candles_for_close_order
                    ),
                }
            else:
                refer_bot_dict = None
        else:
            refer_bot_dict = None

        return refer_bot_dict

    @staticmethod
    async def update_config_for_percentage(
        bot_config, price_provider, symbol, tick_size
    ):
        if not bot_config.stop_win_percents or not bot_config.stop_loss_percents or not bot_config.start_updown_percents:
            return bot_config

        price = await price_provider.get_price(symbol=symbol)

        bot_config.stop_success_ticks = round((price * (bot_config.stop_win_percents/100))/tick_size)
        bot_config.stop_loss_ticks = round((price * (bot_config.stop_loss_percents/100))/tick_size)
        bot_config.start_updown_ticks = round((price * (bot_config.start_updown_percents/100))/tick_size)

        if bot_config.stop_success_ticks < 1:
            bot_config.stop_success_ticks = 1
        if bot_config.stop_loss_ticks < 1:
            bot_config.stop_loss_ticks = 1
        if bot_config.start_updown_ticks < 1:
            bot_config.start_updown_ticks = 1

        return bot_config

    @staticmethod
    async def get_profitable_bots_id_by_tf(
        bot_crud, bot_profitability_timeframes
    ):
        tf_bot_ids = {}

        for tf in bot_profitability_timeframes:
            time_ago = timedelta(minutes=float(tf))

            profits_data = await bot_crud.get_sorted_by_profit(
                since=time_ago, just_not_copy_bots=True
            )
            filtered_sorted = sorted(
                [item for item in profits_data if item[1] > 0],
                key=lambda x: x[1],
                reverse=True,
            )
            tf_bot_ids[tf] = [item[0] for item in filtered_sorted]

        return tf_bot_ids

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        bot_crud: TestBotCrud = resolve_crud(TestBotCrud),
    ):
        first_run_completed = False
        bot_profitability_timeframes = []

        while not self.stop_event.is_set():
            if not first_run_completed:
                first_run_completed = True

                bot_profitability_timeframes = (
                    await bot_crud.get_unique_copy_bot_min_time_profitability()
                )
                print(bot_profitability_timeframes)
                print('bot_profitability_timeframes')

            tf_bot_ids = await self.get_profitable_bots_id_by_tf(
                bot_crud=bot_crud,
                bot_profitability_timeframes=bot_profitability_timeframes,
            )

            bots = await bot_crud.get_bots_with_profitability_time()

            for bot in bots:
                refer_bot_dict = await self.get_bot_config_by_params(
                    bot_crud=bot_crud,
                    tf_bot_ids=tf_bot_ids,
                    copy_bot_min_time_profitability_min=bot.copy_bot_min_time_profitability_min,
                )
                print(refer_bot_dict)
                print(f"copy_bot_{bot.id}")
                if refer_bot_dict:
                    await redis.set(
                        f"copy_bot_{bot.id}", json.dumps(refer_bot_dict)
                    )

            await asyncio.sleep(30)
