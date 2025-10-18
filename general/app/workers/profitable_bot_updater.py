import asyncio
import json
import logging

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.crud.test_bot import TestBotCrud
from app.db.models import TestBot
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

        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

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
            logging.info('Failed to get copybot data')
            logging.info(e)

        return copy_bot

    @staticmethod
    async def get_bot_config_by_params(
        bot_crud, bot_ids
    ):
        if bot_ids:
            refer_bot = None
            try:
                refer_bot = await bot_crud.get_bot_by_id(
                    bot_id=bot_ids[0]
                )
                logging.info('bot_ids fine')
            except:
                logging.info(bot_ids)
                logging.info('bot_ids error')

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
                    "time_to_wait_for_entry_price_to_open_order_in_seconds": str(
                        refer_bot.time_to_wait_for_entry_price_to_open_order_in_seconds
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

        stop_success_ticks = round((price * (bot_config.stop_win_percents/100))/tick_size)
        stop_loss_ticks = round((price * (bot_config.stop_loss_percents/100))/tick_size)
        start_updown_ticks = round((price * (bot_config.start_updown_percents/100))/tick_size)

        if stop_success_ticks < 1:
            stop_success_ticks = 1
        if stop_loss_ticks < 1:
            stop_loss_ticks = 1
        if start_updown_ticks < 1:
            start_updown_ticks = 1

        ref_bot_config = bot_config.clone()

        ref_bot_config.stop_success_ticks = stop_success_ticks
        ref_bot_config.stop_loss_ticks = stop_loss_ticks
        ref_bot_config.start_updown_ticks = start_updown_ticks

        return ref_bot_config

    @staticmethod
    async def get_profitable_bots_id_by_timeframes(
        bot_crud, bot_profitability_timeframes,
        check_24h_profitability=False,
        by_referral_bot_id=False,
    ):
        tf_bot_ids = {}

        for tf in bot_profitability_timeframes:
            tf_bot_ids[tf] = await ProfitableBotUpdaterCommand.filter_profitable_bots_id(
                bot_crud=bot_crud,
                timeframe=tf,
                check_24h_profitability=check_24h_profitability,
                by_referral_bot_id=by_referral_bot_id,
            )

        return tf_bot_ids

    @staticmethod
    async def get_profitable_bots_id_by_individual_params(
        bot_crud,
        bot_profitability_parameters,
    ):
        tf_bot_ids = {}

        for bot_id, parameters in bot_profitability_parameters.items():
            tf_bot_ids[bot_id] = await ProfitableBotUpdaterCommand.filter_profitable_bots_id(
                bot_crud=bot_crud,
                timeframe=parameters["tf"],
                check_24h_profitability=parameters["24h"],
                by_referral_bot_id=parameters["by_ref"],
            )

        return tf_bot_ids

    @staticmethod
    async def filter_profitable_bots_id(
        bot_crud,
        timeframe,
        check_24h_profitability=False,
        by_referral_bot_id=False,
    ):
        time_ago = timedelta(minutes=float(timeframe))

        profits_data = await bot_crud.get_sorted_by_profit(
            since=time_ago, just_not_copy_bots=True
        )
        filtered_sorted = sorted(
            [item for item in profits_data if item[1] > 0],
            key=lambda x: x[1],
            reverse=True,
        )
        tf_ids = [item[0] for item in filtered_sorted]
        logging.info(f'profits_data: {len(tf_ids)}')

        if check_24h_profitability:
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
            ids_checked_24h = [item for item in tf_ids if item in ids_24h]
            tf_ids = ids_checked_24h

        if by_referral_bot_id:
            profits_data_by_referral = await bot_crud.get_sorted_by_profit(
                since=time_ago, just_not_copy_bots=True, by_referral_bot_id=True
            )
            logging.info(f'profits_data_by_referral: {len(profits_data_by_referral)}')
            filtered_sorted_by_referral = sorted(
                [item for item in profits_data_by_referral if item[1] > 0],
                key=lambda x: x[1],
                reverse=True,
            )
            tf_ids_by_referral = [item[0] for item in filtered_sorted_by_referral]
            logging.info(f'tf_ids_by_referral: {len(tf_ids_by_referral)}')
            ids_checked_by_referral = [item for item in tf_ids if item in tf_ids_by_referral]
            tf_ids = ids_checked_by_referral

        return tf_ids

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        bot_crud: TestBotCrud = resolve_crud(TestBotCrud),
    ):
        first_run_completed = False
        bot_profitability_params = {}

        while not self.stop_event.is_set():
            bots = await bot_crud.get_copybots()

            if not first_run_completed:
                first_run_completed = True

                for bot in bots:
                    params = {
                        'tf': bot.copy_bot_min_time_profitability_min,
                        '24h': bot.copybot_v1_check_for_24h_profitability,
                        'by_ref': bot.copybot_v1_check_for_referral_bot_profitability
                    }
                    bot_profitability_params[bot.id] = params

                logging.info(bot_profitability_params)
                logging.info('bot_profitability_params')

            tf_bot_ids = await self.get_profitable_bots_id_by_individual_params(
                bot_crud=bot_crud,
                bot_profitability_parameters=bot_profitability_params,
            )

            for bot in bots:
                refer_bot_dict = await self.get_bot_config_by_params(
                    bot_crud=bot_crud,
                    bot_ids=tf_bot_ids[bot.id]
                )
                logging.info(refer_bot_dict)
                logging.info(f"copy_bot_{bot.id}")
                if refer_bot_dict:
                    await redis.set(
                        f"copy_bot_{bot.id}", json.dumps(refer_bot_dict)
                    )

            await asyncio.sleep(30)
