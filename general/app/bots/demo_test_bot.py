import asyncio
import threading
import json

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import distinct, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.crud.asset_history import AssetHistoryCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestBot, TestOrder
from app.dependencies import (
    redis_context,
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

UTC = timezone.utc


async def _update_config_from_referral_bot(bot_config: TestBot, redis) -> bool:
    refer_bot_js = await redis.get(f"copy_bot_{bot_config.id}")
    refer_bot = json.loads(refer_bot_js) if refer_bot_js else None

    if not refer_bot:
        print(f"❌ Не удалось найти реферального бота для ID: {bot_config.id}")
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

    return True


async def set_volatile_pairs(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    first_run_completed = False
    asset_volatility_timeframes = []

    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while not stop_event.is_set():
                if not first_run_completed:
                    result = await session.execute(
                        select(
                            distinct(TestBot.min_timeframe_asset_volatility)
                        ).where(
                            TestBot.min_timeframe_asset_volatility.is_not(None)
                        )
                    )
                    unique_values = result.scalars().all()
                    asset_volatility_timeframes = list(unique_values)
                    first_run_completed = True

                most_volatile = None
                tf = None
                symbol = None

                for tf in asset_volatility_timeframes:
                    tf = float(tf)
                    now = datetime.now(UTC)
                    time_ago = now - timedelta(minutes=tf)

                    asset_crud = AssetHistoryCrud(session)
                    most_volatile = await asset_crud.get_most_volatile_since(
                        since=time_ago
                    )

                    if most_volatile:
                        symbol = most_volatile.symbol
                        await redis.set(f"most_volatile_symbol_{tf}", symbol)

                if most_volatile and tf and symbol:
                    print(f"most_volatile_symbol_{tf} updated: {symbol}")

                await asyncio.sleep(30)


async def get_profitable_bots_id_by_tf(session, bot_profitability_timeframes):
    bot_crud = TestBotCrud(session)

    tf_bot_ids = {}
    # profits_data = await bot_crud.get_sorted_by_profit(just_not_copy_bots=True)
    # filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)
    # tf_bot_ids['time'] = [item[0] for item in filtered_sorted]

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


async def get_bot_config_by_params(
    session, tf_bot_ids, copy_bot_min_time_profitability_min
):
    min_bot_ids = tf_bot_ids[copy_bot_min_time_profitability_min]

    if min_bot_ids:
        refer_bot = await session.execute(
            select(TestBot).where(
                TestBot.id == min_bot_ids[0],
                TestBot.min_timeframe_asset_volatility.is_not(None),
            )
        )
        refer_bot = refer_bot.scalars().all()
        if refer_bot:
            refer_bot = refer_bot[0]
            refer_bot_dict = {
                "id": refer_bot.id,
                "symbol": refer_bot.symbol,
                "stop_success_ticks": refer_bot.stop_success_ticks,
                "stop_loss_ticks": refer_bot.stop_loss_ticks,
                "start_updown_ticks": refer_bot.start_updown_ticks,
                "min_timeframe_asset_volatility": float(
                    refer_bot.min_timeframe_asset_volatility
                ),
                "time_to_wait_for_entry_price_to_open_order_in_minutes": float(
                    refer_bot.time_to_wait_for_entry_price_to_open_order_in_minutes
                ),
            }
        else:
            refer_bot_dict = None
    else:
        refer_bot_dict = None

    return refer_bot_dict


async def set_profitable_bots_for_copy_bots(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    first_run_completed = False
    bot_profitability_timeframes = []

    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while not stop_event.is_set():
                if not first_run_completed:
                    first_run_completed = True

                    result = await session.execute(
                        select(
                            distinct(
                                TestBot.copy_bot_min_time_profitability_min
                            )
                        ).where(
                            TestBot.copy_bot_min_time_profitability_min.is_not(
                                None
                            )
                        )
                    )
                    unique_values = result.scalars().all()
                    bot_profitability_timeframes = list(unique_values)

                tf_bot_ids = await get_profitable_bots_id_by_tf(
                    session, bot_profitability_timeframes
                )

                bots = await session.execute(
                    select(TestBot).where(
                        TestBot.copy_bot_min_time_profitability_min.is_not(
                            None
                        )
                    )
                )
                bots = bots.scalars().all()

                for bot in bots:
                    refer_bot_dict = await get_bot_config_by_params(
                        session,
                        tf_bot_ids,
                        bot.copy_bot_min_time_profitability_min,
                    )
                    await redis.set(
                        f"copy_bot_{bot.id}", json.dumps(refer_bot_dict)
                    )

                await asyncio.sleep(30)


def input_listener(loop, stop_event):
    while True:
        cmd = (
            input("👉 Введите 'stop' чтобы остановить бота:\n").strip().lower()
        )
        if cmd == "stop":
            print("🛑 Останавливаем бота...")
            loop.call_soon_threadsafe(stop_event.set)
            break


class StartTestBotsCommand(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    async def command(
        self,
        order_crud: TestOrderCrud = resolve_crud(TestOrderCrud),
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
    ):
        shared_data = {}

        bot_crud = TestBotCrud(session)
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
                            order_crud=order_crud,
                        )
                    except Exception as e:
                        print(f"❌ Ошибка в боте {bot_config.id}: {e}")
                        await asyncio.sleep(1)

            tasks.append(asyncio.create_task(_run_loop()))

        await asyncio.gather(*tasks)

    @staticmethod
    async def simulate_bot(
        redis,
        bot_config: TestBot,
        shared_data,
        stop_event,
        price_provider,
        order_crud,
    ):

        if bot_config.copy_bot_min_time_profitability_min:
            refer_bot_js = await redis.get(f"copy_bot_{bot_config.id}")
            refer_bot = json.loads(refer_bot_js)

            if not refer_bot:
                return

            bot_config.symbol = refer_bot["symbol"]
            bot_config.stop_success_ticks = refer_bot["stop_success_ticks"]
            bot_config.stop_loss_ticks = refer_bot["stop_loss_ticks"]
            bot_config.start_updown_ticks = refer_bot["start_updown_ticks"]
            bot_config.min_timeframe_asset_volatility = refer_bot[
                "min_timeframe_asset_volatility"
            ]
            bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes = refer_bot[
                "time_to_wait_for_entry_price_to_open_order_in_minutes"
            ]

        symbol = await redis.get(
            f"most_volatile_symbol_{bot_config.min_timeframe_asset_volatility}"
        )
        data = shared_data.get(symbol)

        if not data:
            return

        tick_size = data["tick_size"]

        while not stop_event.is_set():
            if bot_config.copy_bot_min_time_profitability_min:
                bot_config_updated = await _update_config_from_referral_bot(
                    bot_config, redis
                )
                if not bot_config_updated:
                    return

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
                timeout = int(
                    bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes
                    * 60
                )
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

            try:
                await order_crud.create(
                    {
                        "asset_symbol": symbol,
                        "order_type": trade_type,
                        "balance": balance,
                        "open_price": open_price,
                        "open_time": order.open_time,
                        "open_fee": order.open_fee,
                        "stop_loss_price": order.stop_loss_price,
                        "bot_id": bot_config.id,
                        "close_price": close_price,
                        "close_time": datetime.now(UTC),
                        "close_fee": order.open_price
                        * Decimal(COMMISSION_CLOSE),
                        "profit_loss": pnl,
                        "is_active": False,
                        "start_updown_ticks": bot_config.start_updown_ticks,
                        "stop_loss_ticks": bot_config.stop_loss_ticks,
                        "stop_success_ticks": bot_config.stop_success_ticks,
                        "stop_reason_event": order.stop_reason_event,
                    }
                )
                await order_crud.session.commit()
            except Exception as e:
                print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")


async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    input_thread = threading.Thread(
        target=input_listener, args=(loop, stop_event)
    )
    input_thread.start()

    await asyncio.gather(
        set_volatile_pairs(stop_event),
        set_profitable_bots_for_copy_bots(stop_event),
        StartTestBotsCommand(stop_event=stop_event).run_async(),
    )

    print("✅ Все боты завершены.")


if __name__ == "__main__":
    asyncio.run(main())
