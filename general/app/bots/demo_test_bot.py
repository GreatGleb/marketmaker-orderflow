import asyncio
import json

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.constants.order import ORDER_QUEUE_KEY
from app.crud.test_bot import TestBotCrud
from app.db.models import TestBot, TestOrder
from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)

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
        shared_data = {}

        active_bots = await bot_crud.get_active_bots()

        price_provider = PriceProvider(redis=redis)

        builder = MarketDataBuilder(session)
        shared_data = await builder.build()

        tasks = []

        for bot in active_bots:

            async def _run_loop(bot_config=bot):
                while not self.stop_event.is_set():
                    try:
                        await self.simulate_bot(
                            bot_config=bot_config,
                            shared_data=shared_data,
                            redis=redis,
                            stop_event=self.stop_event,
                            price_provider=price_provider,
                        )
                    except Exception as e:
                        print(f"❌ Ошибка в боте {bot_config.id}: {e}")
                        await asyncio.sleep(1)

            tasks.append(asyncio.create_task(_run_loop()))

        await asyncio.gather(*tasks)

    @staticmethod
    async def update_config_from_referral_bot(
        bot_config: TestBot, redis
    ) -> bool:
        refer_bot_js = await redis.get(f"copy_bot_{bot_config.id}")
        refer_bot = json.loads(refer_bot_js) if refer_bot_js else None

        if not refer_bot:
            print(
                f"❌ Не удалось найти реферального бота для ID: {bot_config.id}"
            )
            return False

        bot_config.symbol = refer_bot["symbol"]
        bot_config.stop_success_ticks = refer_bot["stop_success_ticks"]
        bot_config.stop_loss_ticks = refer_bot["stop_loss_ticks"]
        bot_config.start_updown_ticks = refer_bot["start_updown_ticks"]
        bot_config.min_timeframe_asset_volatility = refer_bot[
            "min_timeframe_asset_volatility"
        ]
        bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes = (
            refer_bot["time_to_wait_for_entry_price_to_open_order_in_minutes"]
        )
        bot_config.referral_bot_id = refer_bot["id"]

        return True

    @staticmethod
    def json_serializer(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    async def simulate_bot(
        self,
        redis,
        bot_config: TestBot,
        shared_data,
        stop_event,
        price_provider,
    ):
        setattr(bot_config, "referral_bot_id", None)

        if bot_config.copy_bot_min_time_profitability_min:
            bot_config_updated = await self.update_config_from_referral_bot(
                bot_config, redis
            )
            if not bot_config_updated:
                return

        symbol = await redis.get(
            f"most_volatile_symbol_{bot_config.min_timeframe_asset_volatility}"
        )
        data = shared_data.get(symbol)

        if not data:
            return

        tick_size = data["tick_size"]

        while not stop_event.is_set():
            if bot_config.copy_bot_min_time_profitability_min:
                bot_config_updated = (
                    await self.update_config_from_referral_bot(
                        bot_config, redis
                    )
                )
                if not bot_config_updated:
                    return

                bot_config = await ProfitableBotUpdaterCommand.update_config_for_percentage(
                    bot_config=bot_config,
                    price_provider=price_provider,
                    symbol=symbol,
                    tick_size=tick_size
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

            try:
                timeout = (
                    Decimal(
                        bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes
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
                    ),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                is_timeout_occurred = True

            if is_timeout_occurred or not trade_type or not entry_price:
                return False

            open_price = entry_price
            price_from_previous_step = entry_price

            close_not_lose_price = (
                PriceCalculator.calculate_close_not_lose_price(
                    open_price=open_price, trade_type=trade_type
                )
            )
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
                stop_success_ticks=bot_config.stop_success_ticks,
                open_price=open_price,
                open_time=datetime.now(UTC),
                open_fee=(
                    Decimal(bot_config.balance) * Decimal(COMMISSION_OPEN)
                ),
            )

            while not stop_event.is_set():
                updated_price = await price_provider.get_price(symbol=symbol)

                new_tk_p = PriceCalculator.calculate_take_profit_price(
                    stop_success_ticks=bot_config.stop_success_ticks,
                    tick_size=tick_size,
                    open_price=updated_price,
                    trade_type=trade_type,
                )
                new_sl_p = PriceCalculator.calculate_stop_lose_price(
                    stop_loss_ticks=bot_config.stop_loss_ticks,
                    tick_size=tick_size,
                    trade_type=trade_type,
                    open_price=updated_price,
                )

                should_exit, take_profit_price = (
                    ExitStrategy.check_exit_conditions(
                        trade_type=trade_type,
                        price_from_previous_step=price_from_previous_step,
                        updated_price=updated_price,
                        new_tk_p=new_tk_p,
                        new_sl_p=new_sl_p,
                        close_not_lose_price=close_not_lose_price,
                        take_profit_price=take_profit_price,
                        order=order,
                    )
                )

                if should_exit:
                    break

                price_from_previous_step = updated_price

                await asyncio.sleep(0.1)

            close_price = await price_provider.get_price(symbol=symbol)

            balance = bot_config.balance

            pnl = PriceCalculator.calculate_pnl(
                balance=balance,
                close_price=close_price,
                open_price=open_price,
                trade_type=trade_type,
            )
            order_data = {
                "asset_symbol": symbol,
                "order_type": trade_type,
                "balance": str(balance),
                "open_price": str(open_price),
                "open_time": order.open_time,
                "open_fee": str(order.open_fee),
                "stop_loss_price": str(order.stop_loss_price),
                "bot_id": bot_config.id,
                "close_price": str(close_price),
                "close_time": datetime.now(UTC),
                "close_fee": str(order.open_price * Decimal(COMMISSION_CLOSE)),
                "profit_loss": str(pnl),
                "is_active": False,
                "start_updown_ticks": bot_config.start_updown_ticks,
                "stop_loss_ticks": bot_config.stop_loss_ticks,
                "stop_success_ticks": bot_config.stop_success_ticks,
                "stop_reason_event": order.stop_reason_event,
                "referral_bot_id": bot_config.referral_bot_id,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
            await redis.rpush(
                ORDER_QUEUE_KEY,
                json.dumps(order_data, default=self.json_serializer),
            )
