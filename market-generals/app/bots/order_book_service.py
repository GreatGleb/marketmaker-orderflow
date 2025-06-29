import asyncio
import enum
import threading
from datetime import datetime, timezone
from decimal import Decimal

from app.crud.asset_order_book import AssetOrderBookCrud
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


async def simulate_bot(session, bot_config: TestBot, shared_data, redis):
    symbol = bot_config.symbol
    data = shared_data.get(symbol)

    if not data:
        print(f"❌ Нет данных по символу {symbol}")
        return

    tick_size = data["tick_size"]

    while True:
        current_price = await get_price_from_redis(redis, symbol)
        best_bid = float(data["order_book"].bids[0][0])
        best_ask = float(data["order_book"].asks[0][0])

        if current_price >= best_ask:
            trade_type = TradeType.BUY
        elif current_price <= best_bid:
            trade_type = TradeType.SELL
        else:
            await asyncio.sleep(0.1)
            continue

        min_move_size = bot_config.take_profit_ticks * tick_size

        stop_loss = (
            current_price - min_move_size
            if trade_type == TradeType.BUY
            else current_price + min_move_size
        )

        print(
            f"⏳ Бот {bot_config.id} | {trade_type} | "
            f"Текущая: {current_price:.4f} | Ждём: {stop_loss:.4f}"
        )

        order = TestOrder(
            stop_loss_price=Decimal(stop_loss),
            open_price=current_price,
            open_time=datetime.now(UTC),
            open_fee=Decimal(current_price) * Decimal(COMMISSION_OPEN),
        )

        while True:
            updated_price = await get_price_from_redis(redis, symbol)

            stop_loss = (
                updated_price - min_move_size
                if trade_type == TradeType.BUY
                else updated_price + min_move_size
            )

            last_profit_price = (
                order.stop_loss_price + min_move_size
                if trade_type == TradeType.BUY
                else order.stop_loss_price - min_move_size
            )

            if trade_type == TradeType.BUY:
                if updated_price <= order.stop_loss_price:
                    break
                if updated_price > last_profit_price:
                    order.stop_loss_price = stop_loss
            else:
                if updated_price >= order.stop_loss_price:
                    break
                if updated_price < last_profit_price:
                    order.stop_loss_price = stop_loss

            await asyncio.sleep(0.1)

        close_price = await get_price_from_redis(redis, symbol)
        balance = bot_config.balance
        close_commission = balance * COMMISSION_CLOSE
        open_commission = balance * COMMISSION_OPEN

        if trade_type == TradeType.BUY:
            amount = Decimal(bot_config.balance) / Decimal(order.open_price)

            revenue = amount * Decimal(close_price)

            total_commission = Decimal(open_commission) + Decimal(
                close_commission
            )

            pnl = revenue - Decimal(balance) - total_commission

        else:
            total_commission = Decimal(open_commission) + Decimal(
                close_commission
            )

            amount = Decimal(balance) / Decimal(order.open_price)

            cost = amount * Decimal(close_price)

            pnl = Decimal(balance) - cost - total_commission

        print(f"🔎 Бот {bot_config.id} | {trade_type} | PNL: {pnl:.4f}")

        try:
            await TestOrderCrud(session).create(
                {
                    "asset_symbol": symbol,
                    "order_type": trade_type,
                    "balance": balance,
                    "open_price": current_price,
                    "open_time": order.open_time,
                    "open_fee": order.open_fee,
                    "stop_loss_price": order.stop_loss_price,
                    "bot_id": bot_config.id,
                    "close_price": close_price,
                    "close_time": datetime.now(UTC),
                    "close_fee": order.open_price * Decimal(COMMISSION_OPEN),
                    "profit_loss": pnl,
                    "is_active": False,
                }
            )
            await session.commit()

            print(
                f"💬 Бот {bot_config.id} | {trade_type} | "
                f"Entry: {current_price:.4f} | "
                f"Close: {close_price:.4f} | Amount: {amount:.6f} | "
                f"PnL: {pnl:.4f} | TP_ticks: "
                f"{bot_config.take_profit_ticks} | "
                f"SL_ticks: {bot_config.stop_loss_ticks}"
            )

        except Exception as e:
            print(f"❌ Ошибка при записи ордера бота {bot_config.id}: {e}")


async def simulate_multiple_bots():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    shared_data = {}

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)

        active_bots = await bot_crud.get_active_bots()
        order_book_crud = AssetOrderBookCrud(session)
        exchange_crud = AssetExchangeSpecCrud(session)

        symbols = {bot.symbol for bot in active_bots}

        for symbol in symbols:
            order_book = await order_book_crud.get_latest_by_symbol(symbol)
            step_sizes = await exchange_crud.get_step_size_by_symbol(symbol)
            shared_data[symbol] = {
                "order_book": order_book,
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


def main():
    input_thread = threading.Thread(target=input_listener)
    input_thread.start()
    asyncio.run(simulate_multiple_bots())
    print("✅ Все боты завершены.")


if __name__ == "__main__":
    main()
