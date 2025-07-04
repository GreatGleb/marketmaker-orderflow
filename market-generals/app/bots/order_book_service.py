import asyncio
import enum
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestBot, TestOrder
from app.dependencies import redis_context

UTC = timezone.utc
COMMISSION_OPEN = Decimal("0.0002")
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

    if trade_type == 'buy':
        commission_open_cost = 1 + COMMISSION_OPEN
        commission_close_cost = 1 - COMMISSION_CLOSE
        base_take_profit = open_price * commission_open_cost + desired_net_profit_value
        take_profit_price = base_take_profit / commission_close_cost
    else:
        commission_open_cost = 1 - COMMISSION_OPEN
        commission_close_cost = 1 + COMMISSION_CLOSE
        base_take_profit = open_price * commission_open_cost - desired_net_profit_value
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
    if trade_type == 'buy':
        commission_open_cost = 1 + COMMISSION_OPEN
        commission_close_cost = 1 - COMMISSION_CLOSE
    else:
        commission_open_cost = 1 - COMMISSION_OPEN
        commission_close_cost = 1 + COMMISSION_CLOSE

    close_not_lose_price = (open_price * commission_open_cost) / commission_close_cost

    return close_not_lose_price

async def simulate_bot(session, redis, bot_config: TestBot, shared_data, stop_event):
    symbol = await redis.get("most_volatile_symbol")
    # symbol = bot_config.symbol
    data = shared_data.get(symbol)

    if not data:
        # print(f"❌ Нет данных по символу {symbol}")
        return

    tick_size = data["tick_size"]

    while not stop_event.is_set():
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
            trade_type, entry_price = await asyncio.wait_for(
                _wait_for_entry_price(
                    redis, symbol, entry_price_buy, entry_price_sell
                ),
                timeout=30
            )
        except asyncio.TimeoutError:
            timeoutOccurred = True

        if timeoutOccurred or not trade_type or not entry_price:
            # print(f"Bot {bot_config.id}; A minute has passed, entry conditions have not been met")
            return False

        open_price = entry_price
        close_not_lose_price = calculate_close_not_lose_price(open_price, trade_type)
        stop_loss_price = calculate_stop_lose_price(bot_config, tick_size, open_price, trade_type)
        take_profit_price = calculate_take_profit_price(bot_config, tick_size, open_price, trade_type)

        print(
            f"🔎 Бот {bot_config.id} | {trade_type} | Вход: {open_price:.4f} | "
            f"SL: {stop_loss_price:.4f} | TP: {take_profit_price:.4f}"
        )

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
            itWasHigher_tk = False

            if trade_type == TradeType.BUY:
                if updated_price > take_profit_price:
                    itWasHigher_tk = True
                if new_tk_p > take_profit_price:
                    take_profit_price = new_tk_p
                elif new_sl_p > order.stop_loss_price:
                    order.stop_loss_price = new_sl_p
                if updated_price <= order.stop_loss_price:
                    print(f"Бот {bot_config.id} | 📉⛔ BUY order closed by STOP-LOSE at {updated_price}")
                    break
                if itWasHigher_tk and updated_price > close_not_lose_price and updated_price <= take_profit_price:
                    print(f"Бот {bot_config.id} | 📈✅ BUY order closed by STOP-WIN at {updated_price}, Take profit: {take_profit_price}")
                    break
            else:
                if updated_price < take_profit_price:
                    itWasHigher_tk = True
                if new_tk_p < take_profit_price:
                    take_profit_price = new_tk_p
                elif new_sl_p < order.stop_loss_price:
                    order.stop_loss_price = new_sl_p
                if updated_price >= order.stop_loss_price:
                    print(f"Бот {bot_config.id} | 📉⛔ SELL order closed by STOP-LOSE at {updated_price}")
                    break
                if itWasHigher_tk and updated_price < close_not_lose_price and updated_price >= take_profit_price:
                    print(f"Бот {bot_config.id} | 📈✅ SELL order closed by STOP-WIN at {updated_price}, Take profit: {take_profit_price}")
                    break

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
                }
            )
            await session.commit()
        except Exception as e:
            print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")

async def set_volatile_pairs(stop_event):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while not stop_event.is_set():
                now = datetime.now(UTC)
                time_ago = now - timedelta(minutes=1)

                # 1. Найти наиболее волатильную пару
                asset_crud = AssetHistoryCrud(session)
                most_volatile = await asset_crud.get_most_volatile_since(
                    since=time_ago
                )

                if most_volatile:
                    symbol = most_volatile.symbol
                    await redis.set("most_volatile_symbol", symbol)
                    print(f"most_volatile_symbol updated: {symbol}")
                await asyncio.sleep(30)

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
        simulate_multiple_bots(stop_event),
        set_volatile_pairs(stop_event)
    )

    print("✅ Все боты завершены.")

if __name__ == "__main__":
    asyncio.run(main())
