import asyncio
import json
import logging
import traceback
from collections import namedtuple

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.config import settings
from app.bots.binance_bot import BinanceBot
from app.constants.order import ORDER_QUEUE_KEY
from app.crud.test_bot import TestBotCrud
from app.db.models import TestBot, TestOrder
from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)
from app.db.base import DatabaseSessionManager
from app.constants.commissions import COMMISSION_OPEN, COMMISSION_CLOSE
from app.sub_services.logic.market_setup import MarketDataBuilder
from app.sub_services.logic.price_calculator import PriceCalculator
from app.sub_services.watchers.price_provider import (
    PriceWatcher,
    PriceProvider,
)
from app.utils import Command
from app.sub_services.logic.exit_strategy import ExitStrategy
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand
from app.sub_services.notifications.factory import NotificationServiceFactory

UTC = timezone.utc


class StartTestBotsCommand(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        bot_crud: TestBotCrud = resolve_crud(TestBotCrud),
    ):
        active_bots = await bot_crud.get_active_bots()

        price_provider = PriceProvider(redis=redis)
        binance_bot = BinanceBot(is_need_prod_for_data=True, redis=redis)

        builder = MarketDataBuilder(session)
        shared_data = await builder.build()

        logging.info(shared_data)

        tasks = []

        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        bot_ids = {bot.id for bot in active_bots}
        bot_ids = list(bot_ids)
        logging.info(bot_ids)


        active_bots = await bot_crud.get_active_bots()
        active_bots_dicts = [bot.__dict__ for bot in active_bots]
        active_bots_dicts = [{k: v for k, v in bot_dict.items() if k != '_sa_instance_state'} for bot_dict in
                                   active_bots_dicts]

        BotObject = namedtuple('BotObject', active_bots_dicts[0].keys())
        active_bots_tuples = [BotObject(**bot) for bot in active_bots_dicts]
        logging.info(active_bots_tuples)

        for bot in active_bots_tuples:
            if bot.id == 1:
                async def _run_loop(bot_config):
                    while not self.stop_event.is_set():
                        try:
                            await self.simulate_bot(
                                original_bot_config=bot_config,
                                shared_data=shared_data,
                                redis=redis,
                                stop_event=self.stop_event,
                                price_provider=price_provider,
                                binance_bot=binance_bot,
                                bot_crud=bot_crud,
                            )
                        except Exception as e:
                            try:
                                error_traceback = traceback.format_exc()
                                logging.info(error_traceback)
                                telegram_service = (
                                    NotificationServiceFactory.get_telegram_service()
                                )
                                if telegram_service:
                                    await telegram_service.send_bot_error_notification(
                                        bot_id=bot_config.id,
                                        error_message=str(e),
                                        additional_info=f"Полный стек ошибки:\n{error_traceback}",
                                    )
                            except Exception as telegram_error:
                                logging.info(
                                    f"❌ Ошибка при отправке уведомления в Telegram: {telegram_error}"
                                )
                            await asyncio.sleep(1)

                tasks.append(asyncio.create_task(_run_loop(bot)))

        await asyncio.gather(*tasks)

    @staticmethod
    async def update_config_from_referral_bot(
        bot_config: TestBot, bot_crud, redis, session, is_it_copy_bot_v2 = False,
    ):
        if is_it_copy_bot_v2:
            tf_bot_ids = await ProfitableBotUpdaterCommand.get_profitable_bots_id_by_tf(
                bot_crud=bot_crud,
                bot_profitability_timeframes=[bot_config.copy_bot_min_time_profitability_min],
                by_referral_bot_id=True,
            )

            logging.info('finished get_profitable_bots_id_by_tf')
            refer_bot = await ProfitableBotUpdaterCommand.get_bot_config_by_params(
                bot_crud=bot_crud,
                tf_bot_ids=tf_bot_ids,
                copy_bot_min_time_profitability_min=bot_config.copy_bot_min_time_profitability_min
            )
        else:
            refer_bot_js = await redis.get(f"copy_bot_{bot_config.id}")
            refer_bot = json.loads(refer_bot_js) if refer_bot_js else None

        if not refer_bot:
            logging.info(
                f"❌ Не удалось найти реферального бота для ID: {bot_config.id}"
            )
            return {
                'config': False,
                'referral_bot_id': 0
            }

        ref_bot_config = TestBot(
            balance=1000,
            symbol=refer_bot["symbol"],
            stop_success_ticks=Decimal(refer_bot['stop_success_ticks'] or 0),
            stop_loss_ticks=Decimal(refer_bot['stop_loss_ticks'] or 0),
            start_updown_ticks=Decimal(refer_bot['start_updown_ticks'] or 0),
            stop_win_percents=Decimal(refer_bot['stop_win_percents']),
            stop_loss_percents=Decimal(refer_bot['stop_loss_percents']),
            start_updown_percents=Decimal(refer_bot['start_updown_percents']),
            min_timeframe_asset_volatility=refer_bot['min_timeframe_asset_volatility'],
            time_to_wait_for_entry_price_to_open_order_in_minutes=Decimal(refer_bot[
                'time_to_wait_for_entry_price_to_open_order_in_minutes'
            ]),
            consider_ma_for_open_order=bool(refer_bot['consider_ma_for_open_order']),
            consider_ma_for_close_order=bool(refer_bot['consider_ma_for_close_order']),
            ma_number_of_candles_for_open_order=refer_bot['ma_number_of_candles_for_open_order'],
            ma_number_of_candles_for_close_order=refer_bot['ma_number_of_candles_for_close_order'],
        )

        # for test if copy_bot use right refer_bot
        # tf_bot_ids = (
        #     await ProfitableBotUpdaterCommand.get_profitable_bots_id_by_tf(
        #         bot_crud=bot_crud,
        #         bot_profitability_timeframes=[
        #             bot_config.copy_bot_min_time_profitability_min
        #         ],
        #     )
        # )
        #
        # refer_bot = await ProfitableBotUpdaterCommand.get_bot_config_by_params(
        #     bot_crud=bot_crud,
        #     tf_bot_ids=tf_bot_ids,
        #     copy_bot_min_time_profitability_min=bot_config.copy_bot_min_time_profitability_min,
        # )
        #
        # if refer_bot:
        #     bot_config.referral_bot_from_profit_func = refer_bot["id"]

        return {
            'config': ref_bot_config,
            'referral_bot_id': refer_bot['id']
        }

    @staticmethod
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    async def simulate_bot(
        self,
        redis,
        original_bot_config: TestBot,
        shared_data,
        stop_event,
        price_provider,
        binance_bot,
        bot_crud,
    ):
        while not stop_event.is_set():
            dsm = DatabaseSessionManager.create(settings.DB_URL)
            async with dsm.get_session() as session:
                # setattr(bot_config, "referral_bot_from_profit_func", None)
                referral_bot_id = None
                bot_id = original_bot_config.id
                bot_config = None

                if original_bot_config.copybot_v2_time_in_minutes:
                    copybot_v2_time_in_minutes = original_bot_config.copybot_v2_time_in_minutes
                    bot_config = (
                        await ProfitableBotUpdaterCommand.get_copybot_config(
                            bot_crud=bot_crud,
                            copybot_v2_time_in_minutes=copybot_v2_time_in_minutes
                        )
                    )

                    if not bot_config:
                        logging.info(f'there no copybot_v2 ref {bot_id}')
                        await asyncio.sleep(60)
                        return

                if not bot_config:
                    bot_config = original_bot_config

                is_it_copy = bot_config.copy_bot_min_time_profitability_min

                if is_it_copy:
                    updating_config_res = (
                        await self.update_config_from_referral_bot(
                            bot_config=bot_config, bot_crud=bot_crud, redis=redis,
                            is_it_copy_bot_v2=(original_bot_config.copybot_v2_time_in_minutes is not None),
                            session=session
                        )
                    )
                    bot_config = updating_config_res['config']
                    referral_bot_id = updating_config_res['referral_bot_id']

                    if not bot_config:
                        await asyncio.sleep(60)
                        return

                    logging.info(f'found ref for {bot_id}')

                # if bot_config.consider_ma_for_open_order:
                #     symbol = bot_config.symbol
                # else:
                #     symbol = await redis.get(
                #         f"most_volatile_symbol_{bot_config.min_timeframe_asset_volatility}"
                #     )

                symbol = bot_config.symbol

                if not symbol:
                    logging.info('there no symbol')
                    await asyncio.sleep(60)
                    return

                data = shared_data.get(symbol)
                if not data:
                    logging.info(original_bot_config.copybot_v2_time_in_minutes)
                    logging.info('original_bot_config.copybot_v2_time_in_minutes')
                    logging.info(is_it_copy)
                    logging.info('is_it_copy')
                    logging.info(symbol)
                    logging.info('not symbols data')
                    await asyncio.sleep(60)
                    return
                tick_size = data["tick_size"]

                if bot_id == 1:
                    logging.info('bot_id 1 started work')

                bot_config = (
                    await ProfitableBotUpdaterCommand.update_config_for_percentage(
                        bot_config=bot_config,
                        price_provider=price_provider,
                        symbol=symbol,
                        tick_size=tick_size,
                    )
                )

                initial_price = await price_provider.get_price(symbol=symbol)

                entry_price_buy = (
                    initial_price + bot_config.start_updown_ticks * tick_size
                )
                entry_price_sell = (
                    initial_price - bot_config.start_updown_ticks * tick_size
                )

                is_timeout_occurred = False

                trade_type = None
                entry_price = None

                if is_it_copy or bot_id == 1:
                    logging.info(f'waiting for {bot_id}')

                try:
                    wait_minutes = 1
                    if bot_config.consider_ma_for_open_order:
                        wait_minutes = 12 * 60

                    if bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes:
                        wait_minutes = bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes

                    timeout = (
                        Decimal(
                            wait_minutes
                        )
                        * 60
                    )
                    timeout = int(timeout)
                    price_watcher = PriceWatcher(redis=redis)

                    trade_type, entry_price = await asyncio.wait_for(
                        price_watcher.wait_for_entry_price(
                            symbol=symbol,
                            entry_price_buy=entry_price_buy,
                            entry_price_sell=entry_price_sell,
                            binance_bot=binance_bot,
                            bot_config=bot_config,
                        ),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    is_timeout_occurred = True

                if is_it_copy or bot_id == 1:
                    logging.info(f'is_timeout_occurred: {is_timeout_occurred} for {bot_id}')

                if is_timeout_occurred or not trade_type or not entry_price:
                    return False

                open_price = entry_price
                price_from_previous_step = entry_price

                close_not_lose_price = (
                    PriceCalculator.calculate_close_not_lose_price(
                        open_price=open_price, trade_type=trade_type
                    )
                )

                if not bot_config.consider_ma_for_close_order:
                    stop_loss_price = PriceCalculator.calculate_stop_lose_price(
                        stop_loss_ticks=bot_config.stop_loss_ticks,
                        tick_size=tick_size,
                        open_price=open_price,
                        trade_type=trade_type,
                    )
                    original_take_profit_price = (
                        PriceCalculator.calculate_take_profit_price(
                            stop_success_ticks=bot_config.stop_success_ticks,
                            tick_size=tick_size,
                            open_price=open_price,
                            trade_type=trade_type,
                        )
                    )
                    take_profit_price = original_take_profit_price

                    order = TestOrder(
                        stop_loss_price=Decimal(stop_loss_price),
                        start_updown_ticks=bot_config.start_updown_ticks,
                        stop_success_ticks=bot_config.stop_success_ticks,
                        stop_loss_ticks=bot_config.stop_loss_ticks,
                        open_price=open_price,
                        open_time=datetime.now(UTC),
                        open_fee=(
                            Decimal(bot_config.balance) * Decimal(COMMISSION_OPEN)
                        ),
                        order_type=trade_type
                    )
                else:
                    order = TestOrder(
                        stop_loss_price=0,
                        start_updown_ticks=0,
                        stop_success_ticks=0,
                        stop_loss_ticks=0,
                        open_price=open_price,
                        open_time=datetime.now(UTC),
                        open_fee=(
                            Decimal(bot_config.balance) * Decimal(COMMISSION_OPEN)
                        ),
                        order_type=trade_type
                    )

                if is_it_copy or bot_id == 1:
                    logging.info(f'wait for should_exit for {bot_id}')

                while not stop_event.is_set():
                    updated_price = await price_provider.get_price(symbol=symbol)

                    if bot_config.consider_ma_for_close_order:
                        should_exit = (
                            await ExitStrategy.check_exit_ma_conditions(
                                binance_bot=binance_bot,
                                bot_config=bot_config,
                                symbol=symbol,
                                order_side=order.order_type,
                                updated_price=updated_price,
                                close_not_lose_price=close_not_lose_price,
                            )
                        )
                    else:
                        should_exit, take_profit_price = (
                            await ExitStrategy.check_exit_ticks_conditions(
                                bot_config=bot_config,
                                price_calculator=PriceCalculator,
                                tick_size=tick_size,
                                order=order,
                                close_not_lose_price=close_not_lose_price,
                                take_profit_price=take_profit_price,
                                updated_price=updated_price,
                                price_from_previous_step=price_from_previous_step,
                            )
                        )

                    if should_exit:
                        break

                    price_from_previous_step = updated_price

                    await asyncio.sleep(0.1)

                if is_it_copy or bot_id == 1:
                    logging.info(f'end wait for {bot_id}')

                close_price = await price_provider.get_price(symbol=symbol)

                balance = bot_config.balance

                pnl = PriceCalculator.calculate_pnl(
                    balance=balance,
                    close_price=close_price,
                    open_price=open_price,
                    trade_type=trade_type,
                )
                logging.info(f'creating order {bot_id}')
                order_data = {
                    "asset_symbol": symbol,
                    "order_type": trade_type,
                    "balance": str(balance),
                    "open_price": str(open_price),
                    "open_time": order.open_time,
                    "open_fee": str(order.open_fee),
                    "stop_loss_price": str(order.stop_loss_price),
                    "bot_id": bot_id,
                    "close_price": str(close_price),
                    "close_time": datetime.now(UTC),
                    "close_fee": str(order.open_price * Decimal(COMMISSION_CLOSE)),
                    "profit_loss": str(pnl),
                    "is_active": False,
                    "start_updown_ticks": int(order.start_updown_ticks),
                    "stop_loss_ticks": int(order.stop_loss_ticks),
                    "stop_success_ticks": int(order.stop_success_ticks),
                    "stop_reason_event": order.stop_reason_event,
                    "referral_bot_id": referral_bot_id,
                    # "referral_bot_from_profit_func": bot_config.referral_bot_from_profit_func,
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                }
                await redis.rpush(
                    ORDER_QUEUE_KEY,
                    json.dumps(order_data, default=self.json_serializer),
                )
                logging.info(f'end {bot_id}')
