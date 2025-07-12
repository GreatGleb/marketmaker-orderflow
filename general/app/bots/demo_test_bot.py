import asyncio
import enum
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import distinct, select
import json

from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestBot, TestOrder
from app.dependencies import redis_context

UTC = timezone.utc
COMMISSION_OPEN  = Decimal("0.0005")# 0.0002
COMMISSION_CLOSE = Decimal("0.0005")


class TradeType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"

async def get_price_from_redis(redis, symbol: str) -> Decimal:
    while True:
        try:
            price_str = await redis.get(f"price:{symbol}")
            if price_str is not None:
                return Decimal(price_str)
        except Exception as e:
            print(f"❌ Redis error: {e}")
        await asyncio.sleep(0.1)

async def _wait_for_entry_price(redis_conn, symbol, entry_price_buy, entry_price_sell):
    while True:
        current_price = await get_price_from_redis(redis_conn, symbol)

        if current_price >= entry_price_buy:
            return (TradeType.BUY, current_price)
        elif current_price <= entry_price_sell:
            return (TradeType.SELL, current_price)

        await asyncio.sleep(0.1)

def calculate_take_profit_price(bot_config, tick_size, open_price, trade_type):
    desired_net_profit_value = Decimal(bot_config.stop_success_ticks) * tick_size

    if trade_type == TradeType.BUY:
        commission_open_cost = 1 + COMMISSION_OPEN
        commission_close_cost = 1 - COMMISSION_CLOSE
        base_take_profit = open_price * commission_open_cost - desired_net_profit_value
        take_profit_price = base_take_profit / commission_close_cost
    else:
        commission_open_cost = 1 - COMMISSION_OPEN
        commission_close_cost = 1 + COMMISSION_CLOSE
        base_take_profit = open_price * commission_open_cost + desired_net_profit_value
        take_profit_price = base_take_profit / commission_close_cost

    take_profit_price = take_profit_price.quantize(tick_size, rounding=ROUND_HALF_UP)

    return take_profit_price

def calculate_stop_lose_price(bot_config, tick_size, open_price, trade_type):
    stop_loss_price = (
        open_price - bot_config.stop_loss_ticks * tick_size
        if trade_type == TradeType.BUY
        else open_price + bot_config.stop_loss_ticks * tick_size
    )

    return stop_loss_price

def calculate_close_not_lose_price(open_price, trade_type):
    if trade_type == TradeType.BUY:
        commission_open_cost = 1 + COMMISSION_OPEN
        commission_close_cost = 1 - COMMISSION_CLOSE
    else:
        commission_open_cost = 1 - COMMISSION_OPEN
        commission_close_cost = 1 + COMMISSION_CLOSE

    close_not_lose_price = (open_price * commission_open_cost) / commission_close_cost

    return close_not_lose_price

async def _update_config_from_referral_bot(bot_config: TestBot, redis) -> bool:
    refer_bot_js = await redis.get(f"copy_bot_{bot_config.id}")
    refer_bot = json.loads(refer_bot_js) if refer_bot_js else None

    if not refer_bot:
        print(f"❌ Не удалось найти реферального бота для ID: {bot_config.id}")
        return False

    bot_config.symbol = refer_bot['symbol']
    bot_config.stop_success_ticks = refer_bot['stop_success_ticks']
    bot_config.stop_loss_ticks = refer_bot['stop_loss_ticks']
    bot_config.start_updown_ticks = refer_bot['start_updown_ticks']
    bot_config.min_timeframe_asset_volatility = refer_bot['min_timeframe_asset_volatility']
    bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes = refer_bot['time_to_wait_for_entry_price_to_open_order_in_minutes']

    return True

async def simulate_bot(session, redis, bot_config: TestBot, shared_data, stop_event):
    symbol = await redis.get(f"most_volatile_symbol_{bot_config.min_timeframe_asset_volatility}")
    # symbol = bot_config.symbol
    data = shared_data.get(symbol)

    if not data:
        # print(f"❌ Нет данных по символу {symbol}")
        return

    tick_size = data["tick_size"]

    while not stop_event.is_set():
        if bot_config.copy_bot_min_time_profitability_min:
            bot_config_updated = await _update_config_from_referral_bot(bot_config, redis)
            if not bot_config_updated:
                return

        initial_price = await get_price_from_redis(redis, symbol)

        entry_price_buy = (
            initial_price + bot_config.start_updown_ticks * tick_size
        )
        entry_price_sell = (
            initial_price - bot_config.start_updown_ticks * tick_size
        )

        # print(
        #     f"⏳ Бот {bot_config.id} | Ожидаем входа:"
        #     f" BUY ≥ {entry_price_buy:.4f}, SELL ≤ {entry_price_sell:.4f}"
        # )

        timeoutOccurred = False

        try:
            timeout = int(bot_config.time_to_wait_for_entry_price_to_open_order_in_minutes * 60)

            trade_type, entry_price = await asyncio.wait_for(
                _wait_for_entry_price(
                    redis, symbol, entry_price_buy, entry_price_sell
                ),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            timeoutOccurred = True

        if timeoutOccurred or not trade_type or not entry_price:
            # print(f"Bot {bot_config.id}; A minute has passed, entry conditions have not been met")
            return False

        open_price = entry_price
        priceFromPreviousStep = entry_price
        close_not_lose_price = calculate_close_not_lose_price(open_price, trade_type)
        stop_loss_price = calculate_stop_lose_price(bot_config, tick_size, open_price, trade_type)
        original_take_profit_price = calculate_take_profit_price(bot_config, tick_size, open_price, trade_type)
        take_profit_price = original_take_profit_price

        # print(
        #     f"🔎 Бот {bot_config.id} | {trade_type} | Вход: {open_price:.4f} | "
        #     f"SL: {stop_loss_price:.4f} | TP: {take_profit_price:.4f}"
        # )

        order = TestOrder(
            stop_loss_price=Decimal(stop_loss_price),
            stop_success_ticks=bot_config.stop_success_ticks,
            open_price=open_price,
            open_time=datetime.now(UTC),
            open_fee=(Decimal(bot_config.balance) * Decimal(COMMISSION_OPEN)),
        )

        while not stop_event.is_set():
            updated_price = await get_price_from_redis(redis, symbol)
            new_tk_p = calculate_take_profit_price(bot_config, tick_size, updated_price, trade_type)
            new_sl_p = calculate_stop_lose_price(bot_config, tick_size, updated_price, trade_type)

            if trade_type == TradeType.BUY:
                if priceFromPreviousStep < updated_price and new_tk_p > take_profit_price:
                    take_profit_price = new_tk_p
                elif new_sl_p > order.stop_loss_price:
                    order.stop_loss_price = new_sl_p
                if updated_price <= order.stop_loss_price:
                    order.stop_reason_event = 'stop-losed'
                    # print(f"Бот {bot_config.id} | 📉⛔ BUY order closed by STOP-LOSE at {updated_price}")
                    break
                if updated_price > close_not_lose_price and updated_price <= take_profit_price:
                    order.stop_reason_event = 'stop-won'
                    # print(f"Бот {bot_config.id} | 📈✅ BUY order closed by STOP-WIN at {updated_price}, Take profit: {take_profit_price}")
                    break
            else:
                if priceFromPreviousStep > updated_price and new_tk_p < take_profit_price:
                    take_profit_price = new_tk_p
                elif new_sl_p < order.stop_loss_price:
                    order.stop_loss_price = new_sl_p
                if updated_price >= order.stop_loss_price:
                    order.stop_reason_event = 'stop-losed'
                    # print(f"Бот {bot_config.id} | 📉⛔ SELL order closed by STOP-LOSE at {updated_price}")
                    break
                if updated_price < close_not_lose_price and updated_price >= take_profit_price:
                    order.stop_reason_event = 'stop-won'
                    # print(f"Бот {bot_config.id} | 📈✅ SELL order closed by STOP-WIN at {updated_price}, Take profit: {take_profit_price}")
                    break

            priceFromPreviousStep = updated_price

            await asyncio.sleep(0.1)

        # Закрытие сделки
        close_price = await get_price_from_redis(redis, symbol)
        balance = bot_config.balance
        amount = Decimal(balance) / Decimal(open_price)
        commission_open = amount * open_price * COMMISSION_OPEN
        commission_close = amount * close_price * COMMISSION_CLOSE
        total_commission = commission_open + commission_close

        if trade_type == TradeType.BUY:
            pnl = (amount * close_price) - (amount * open_price) - total_commission
        elif trade_type == TradeType.SELL:
            pnl = (amount * open_price) - (amount * close_price) - total_commission

        # print(
        #     f"💬 Бот {bot_config.id} | {trade_type} "
        #     f"| Entry: {open_price:.4f} | "
        #     f"Close: {close_price:.4f} | PnL: {pnl:.4f}"
        # )
        try:
            await TestOrderCrud(session).create(
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
                    "close_fee": order.open_price * Decimal(COMMISSION_CLOSE),
                    "profit_loss": pnl,
                    "is_active": False,
                    "start_updown_ticks": bot_config.start_updown_ticks,
                    "stop_loss_ticks": bot_config.stop_loss_ticks,
                    "stop_success_ticks": bot_config.stop_success_ticks,
                    "stop_reason_event": order.stop_reason_event,
                }
            )
            await session.commit()
        except Exception as e:
            print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")

async def simulate_multiple_bots(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    shared_data = {}

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)
        active_bots = await bot_crud.get_active_bots()
        exchange_crud = AssetExchangeSpecCrud(session)

        asset_crud = AssetHistoryCrud(session)
        # symbols = {active_bot.symbol for active_bot in active_bots}
        symbols = await asset_crud.get_all_active_pairs()

        for symbol in symbols:
            step_sizes = await exchange_crud.get_step_size_by_symbol(symbol)
            shared_data[symbol] = {
                # "order_book": order_book,
                "tick_size": (
                    Decimal(str(step_sizes.get("tick_size")))
                    if step_sizes
                    else None
                ),
            }

    async with redis_context() as redis:
        tasks = []
        for bot in active_bots:
            async def _run_loop(bot_config=bot):
                while not stop_event.is_set():
                    async with dsm.get_session() as session:
                        try:
                            await simulate_bot(
                                session=session,
                                bot_config=bot_config,
                                shared_data=shared_data,
                                redis=redis,
                                stop_event=stop_event,
                            )
                        except Exception as e:
                            print(f"❌ Ошибка в боте {bot_config.id}: {e}")
                            await asyncio.sleep(1)

            tasks.append(asyncio.create_task(_run_loop()))
        await asyncio.gather(*tasks)

async def set_volatile_pairs(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    first_run_completed = False
    asset_volatility_timeframes = []

    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while not stop_event.is_set():
                if not first_run_completed:
                    result = await session.execute(
                        select(distinct(TestBot.min_timeframe_asset_volatility))
                        .where(
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

        profits_data = await bot_crud.get_sorted_by_profit(since=time_ago, just_not_copy_bots=True)
        filtered_sorted = sorted([item for item in profits_data if item[1] > 0], key=lambda x: x[1], reverse=True)
        tf_bot_ids[tf] = [item[0] for item in filtered_sorted]

    return tf_bot_ids

async def get_bot_config_by_params(session, tf_bot_ids, copy_bot_min_time_profitability_min):
    min_bot_ids = tf_bot_ids[copy_bot_min_time_profitability_min]

    if min_bot_ids:
        refer_bot = await session.execute(
            select(TestBot)
            .where(
                TestBot.id == min_bot_ids[0],
                TestBot.min_timeframe_asset_volatility.is_not(None),
            )
        )
        refer_bot = refer_bot.scalars().all()
        if refer_bot:
            refer_bot = refer_bot[0]
            refer_bot_dict = {
                'id': refer_bot.id,
                'symbol': refer_bot.symbol,
                'stop_success_ticks': refer_bot.stop_success_ticks,
                'stop_loss_ticks': refer_bot.stop_loss_ticks,
                'start_updown_ticks': refer_bot.start_updown_ticks,
                'min_timeframe_asset_volatility': float(refer_bot.min_timeframe_asset_volatility),
                'time_to_wait_for_entry_price_to_open_order_in_minutes': float(refer_bot.time_to_wait_for_entry_price_to_open_order_in_minutes)
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
                        select(distinct(TestBot.copy_bot_min_time_profitability_min))
                        .where(
                            TestBot.copy_bot_min_time_profitability_min.is_not(None)
                        )
                    )
                    unique_values = result.scalars().all()
                    bot_profitability_timeframes = list(unique_values)

                tf_bot_ids = await get_profitable_bots_id_by_tf(session, bot_profitability_timeframes)

                bots = await session.execute(
                    select(TestBot)
                    .where(
                        TestBot.copy_bot_min_time_profitability_min.is_not(None)
                    )
                )
                bots = bots.scalars().all()

                for bot in bots:
                    refer_bot_dict = await get_bot_config_by_params(
                        session,
                        tf_bot_ids,
                        bot.copy_bot_min_time_profitability_min
                    )

                    await redis.set(f"copy_bot_{bot.id}", json.dumps(refer_bot_dict))

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
        simulate_multiple_bots(stop_event),
    )

    print("✅ Все боты завершены.")

if __name__ == "__main__":
    asyncio.run(main())
