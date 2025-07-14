import asyncio
import threading
import json

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.constants.order import ORDER_QUEUE_KEY
from app.crud.asset_history import AssetHistoryCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
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

UTC = timezone.utc


class OrderBulkInsert(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    @staticmethod
    def parse_datetime_fields(order, datetime_fields: list[str]) -> dict:
        for field in datetime_fields:
            if field in order and isinstance(order[field], str):
                order[field] = datetime.fromisoformat(order[field])
        return order

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        crud: TestOrderCrud = resolve_crud(TestOrderCrud),
        redis: Redis = Depends(get_redis),
    ):

        orders = []
        DATETIME_FIELDS = ["open_time", "close_time"]
        BATCH_SIZE = 300

        while not self.stop_event.is_set():
            for _ in range(1000):
                raw = await redis.lpop(ORDER_QUEUE_KEY)
                if raw is None:
                    break

                try:
                    order = json.loads(raw)
                    order = self.parse_datetime_fields(order, DATETIME_FIELDS)
                    orders.append(order)
                except Exception as e:
                    print(f"❌ Ошибка при обработке записи из Redis: {e}")

            for i in range(0, len(orders), BATCH_SIZE):
                batch = orders[i : i + BATCH_SIZE]
                try:
                    await crud.bulk_create(orders=batch)
                except Exception as e:
                    print(f"❌ Ошибка при вставке батча в БД: {e}")

            await asyncio.sleep(30)


class VolatilePair(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    async def command(
        self,
        asset_crud: AssetHistoryCrud = resolve_crud(AssetHistoryCrud),
        session: AsyncSession = Depends(get_session),
        redis: Redis = Depends(get_redis),
        bot_crud: TestBotCrud = resolve_crud(TestBotCrud),
    ):
        first_run_completed = False
        asset_volatility_timeframes = []

        while not self.stop_event.is_set():
            if not first_run_completed:
                unique_values = (
                    await bot_crud.get_unique_min_timeframe_volatility_values()
                )
                asset_volatility_timeframes = list(unique_values)
                first_run_completed = True

            most_volatile = None
            tf = None
            symbol = None

            for tf in asset_volatility_timeframes:
                tf = float(tf)
                now = datetime.now(UTC)
                time_ago = now - timedelta(minutes=tf)

                most_volatile = await asset_crud.get_most_volatile_since(
                    since=time_ago
                )

                if most_volatile:
                    symbol = most_volatile.symbol
                    await redis.set(f"most_volatile_symbol_{tf}", symbol)

            if most_volatile and tf and symbol:
                print(f"most_volatile_symbol_{tf} updated: {symbol}")

            await asyncio.sleep(30)


class ProfitableBotUpdater(Command):

    def __init__(self, stop_event):
        super().__init__()
        self.stop_event = stop_event

    @staticmethod
    async def get_bot_config_by_params(
        bot_crud, tf_bot_ids, copy_bot_min_time_profitability_min
    ):
        min_bot_ids = tf_bot_ids[copy_bot_min_time_profitability_min]

        if min_bot_ids:

            refer_bot = await bot_crud.get_bot_with_volatility_by_id(
                bot_id=min_bot_ids[0]
            )

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
                        or 1
                    ),
                }
            else:
                refer_bot_dict = None
        else:
            refer_bot_dict = None

        return refer_bot_dict

    @staticmethod
    async def get_profitable_bots_id_by_tf(
        bot_crud, bot_profitability_timeframes
    ):

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
            f"most_volatile_symbol_{float(bot_config.min_timeframe_asset_volatility)}"
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
            order_data = {
                "asset_symbol": symbol,
                "order_type": trade_type,
                "balance": float(balance),
                "open_price": float(open_price),
                "open_time": order.open_time,
                "open_fee": float(order.open_fee),
                "stop_loss_price": float(order.stop_loss_price),
                "bot_id": bot_config.id,
                "close_price": float(close_price),
                "close_time": datetime.now(UTC),
                "close_fee": float(
                    order.open_price * Decimal(COMMISSION_CLOSE)
                ),
                "profit_loss": float(pnl),
                "is_active": False,
                "start_updown_ticks": bot_config.start_updown_ticks,
                "stop_loss_ticks": bot_config.stop_loss_ticks,
                "stop_success_ticks": bot_config.stop_success_ticks,
                "stop_reason_event": order.stop_reason_event,
            }
            await redis.rpush(
                ORDER_QUEUE_KEY,
                json.dumps(order_data, default=self.json_serializer),
            )


async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    input_thread = threading.Thread(
        target=input_listener, args=(loop, stop_event)
    )
    input_thread.start()

    await asyncio.gather(
        VolatilePair(stop_event=stop_event).run_async(),
        ProfitableBotUpdater(stop_event=stop_event).run_async(),
        StartTestBotsCommand(stop_event=stop_event).run_async(),
        OrderBulkInsert(stop_event=stop_event).run_async(),
    )

    print("✅ Все боты завершены.")


if __name__ == "__main__":
    asyncio.run(main())
