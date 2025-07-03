import asyncio
import enum
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

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

async def simulate_bot(session, redis, bot_config: TestBot, shared_data):
    symbol = await redis.get("most_volatile_symbol")
    # symbol = bot_config.symbol
    data = shared_data.get(symbol)

    if not data:
        # print(f"❌ Нет данных по символу {symbol}")
        return

    tick_size = data["tick_size"]

    while True:
        initial_price = await get_price_from_redis(redis, symbol)

        entry_price_buy = (
            initial_price + bot_config.start_updown_ticks * tick_size
        )
        entry_price_sell = (
            initial_price - bot_config.start_updown_ticks * tick_size
        )

        print(
            f"⏳ Бот {bot_config.id} | Ожидаем входа:"
            f" BUY ≥ {entry_price_buy:.4f}, SELL ≤ {entry_price_sell:.4f}"
        )

        timeoutOccurred = False

        try:
            trade_type, entry_price = await asyncio.wait_for(
                _wait_for_entry_price(
                    redis, symbol, entry_price_buy, entry_price_sell
                ),
                timeout=60
            )
        except asyncio.TimeoutError:
            timeoutOccurred = True

        if timeoutOccurred or not trade_type or not entry_price:
            print(f"Bot {bot_config.id}; A minute has passed, entry conditions have not been met")
            return False

        open_price = entry_price
        stop_loss_price = (
            open_price - bot_config.stop_loss_ticks * tick_size
            if trade_type == TradeType.BUY
            else open_price + bot_config.stop_loss_ticks * tick_size
        )
        take_profit_price = (
            open_price + bot_config.stop_success_ticks * tick_size
            if trade_type == TradeType.BUY
            else open_price - bot_config.stop_success_ticks * tick_size
        )

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

        while True:
            updated_price = await get_price_from_redis(redis, symbol)

            if trade_type == TradeType.BUY:
                if updated_price > open_price:
                    potential_trailing_profit_sl = updated_price - Decimal(order.stop_success_ticks) * tick_size
                    if potential_trailing_profit_sl > take_profit_price:
                        take_profit_price = potential_trailing_profit_sl
                        print(f"BUY: Trailing stop-win updated to {take_profit_price}")
                if updated_price <= take_profit_price:
                    print(f"BUY order closed by TRAILING STOP-WIN at {updated_price}")
                    break
                if updated_price <= order.stop_loss_price:
                    break
                if updated_price - open_price >= tick_size:
                    new_sl = (
                        updated_price - bot_config.stop_loss_ticks * tick_size
                    )
                    if new_sl > float(order.stop_loss_price):
                        order.stop_loss_price = Decimal(new_sl)
            else:
                if updated_price < order.open_price:
                    potential_trailing_profit_sl = updated_price + Decimal(order.stop_success_ticks) * tick_size
                    if potential_trailing_profit_sl < take_profit_price:
                        take_profit_price = potential_trailing_profit_sl
                        print(f"SELL: Trailing stop-win updated to {take_profit_price}")
                if updated_price >= take_profit_price:
                    print(f"SELL order closed by TRAILING STOP-WIN at {updated_price}")
                    break
                if updated_price >= order.stop_loss_price:
                    break
                if open_price - updated_price >= tick_size:
                    new_sl = (
                        updated_price + bot_config.stop_loss_ticks * tick_size
                    )
                    if new_sl < float(order.stop_loss_price):
                        order.stop_loss_price = Decimal(new_sl)

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

        print(
            f"💬 Бот {bot_config.id} | {trade_type} "
            f"| Entry: {open_price:.4f} | "
            f"Close: {close_price:.4f} | PnL: {pnl:.4f}"
        )
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
                    "start_ticks": bot_config.start_updown_ticks,
                    "stop_loss_ticks": bot_config.stop_loss_ticks,
                    "stop_success_ticks": bot_config.stop_success_ticks,
                }
            )
            await session.commit()
        except Exception as e:
            print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")

async def set_volatile_pairs():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        async with redis_context() as redis:
            while True:
                now = datetime.now(UTC)
                five_minutes_ago = now - timedelta(minutes=5)

                # 1. Найти наиболее волатильную пару
                asset_crud = AssetHistoryCrud(session)
                most_volatile = await asset_crud.get_most_volatile_since(
                    since=five_minutes_ago
                )

                symbol = most_volatile.symbol
                await redis.set("most_volatile_symbol", symbol)
                print('most_volatile_symbol updated')
                await asyncio.sleep(60)

async def simulate_multiple_bots():
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
                while True:
                    async with dsm.get_session() as session:
                        try:
                            await simulate_bot(
                                session=session,
                                bot_config=bot_config,
                                shared_data=shared_data,
                                redis=redis,
                            )
                        except Exception as e:
                            print(f"❌ Ошибка в боте {bot_config.id}: {e}")
                            await asyncio.sleep(1)

            tasks.append(asyncio.create_task(_run_loop()))
        await asyncio.gather(*tasks)


def input_listener():
    while True:
        cmd = (
            input("👉 Введите 'stop' чтобы остановить бота:\n").strip().lower()
        )
        if cmd == "stop":
            print("🛑 Останавливаем бота...")
            break


async def main():
    input_thread = threading.Thread(target=input_listener)
    input_thread.start()

    await asyncio.gather(
        simulate_multiple_bots(),
        set_volatile_pairs()
    )

    print("✅ Все боты завершены.")


if __name__ == "__main__":
    asyncio.run(main())
