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
from app.db.models import TestBot
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

        trade_type = TradeType.BUY if best_ask >= best_bid else TradeType.SELL
        entry_price = (
            current_price + tick_size
            if trade_type == TradeType.BUY
            else current_price - tick_size
        )

        print(
            f"⏳ Бот {bot_config.id} | {trade_type} | "
            f"Текущая: {current_price:.4f} | Ждём: {entry_price:.4f}"
        )

        while True:
            updated_price = await get_price_from_redis(redis, symbol)
            if trade_type == TradeType.BUY and updated_price >= entry_price:
                break
            elif trade_type == TradeType.SELL and updated_price <= entry_price:
                break
            await asyncio.sleep(0.1)

        current_price = updated_price
        open_time = datetime.now(UTC)
        balance = bot_config.balance
        open_fee = balance * COMMISSION_OPEN
        close_fee = balance * COMMISSION_CLOSE

        amount = balance / current_price
        take_profit_ticks = Decimal(bot_config.take_profit_ticks)
        stop_loss_ticks = Decimal(bot_config.stop_loss_ticks)

        tp_price = (
            current_price + take_profit_ticks * tick_size
            if trade_type == TradeType.BUY
            else current_price - take_profit_ticks * tick_size
        )
        sl_price = (
            current_price - stop_loss_ticks * tick_size
            if trade_type == TradeType.BUY
            else current_price + stop_loss_ticks * tick_size
        )

        print(
            f"🔎 Бот {bot_config.id} | {trade_type} | Вход: {current_price:.4f}"
        )

        while True:
            price = await get_price_from_redis(redis, symbol)

            if trade_type == TradeType.BUY:
                if price >= tp_price:
                    close_price = tp_price
                    reason = "TP"
                    break
                elif price <= sl_price:
                    close_price = sl_price
                    reason = "SL"
                    break
            else:
                if price <= tp_price:
                    close_price = tp_price
                    reason = "TP"
                    break
                elif price >= sl_price:
                    close_price = sl_price
                    reason = "SL"
                    break

            await asyncio.sleep(0.1)

        if trade_type == TradeType.BUY:
            revenue = amount * close_price
            pnl = revenue - balance - open_fee - close_fee
        else:
            cost = amount * close_price
            pnl = balance - cost - open_fee - close_fee

        try:
            await TestOrderCrud(session).create(
                {
                    "asset_symbol": symbol,
                    "order_type": trade_type,
                    "balance": balance,
                    "open_price": current_price,
                    "open_time": open_time,
                    "open_fee": open_fee,
                    "stop_loss_price": sl_price,
                    "bot_id": bot_config.id,
                    "close_price": close_price,
                    "close_time": datetime.now(UTC),
                    "close_fee": close_fee,
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
                f"{take_profit_ticks} | "
                f"SL_ticks: {stop_loss_ticks} | Reason: {reason}"
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
