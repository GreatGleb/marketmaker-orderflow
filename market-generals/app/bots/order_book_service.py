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

    while True:
        order_book = data["order_book"]
        tick_size = data["tick_size"]

        if (
            not order_book
            or not order_book.bids
            or not order_book.asks
            or not tick_size
        ):
            print("❌ Нет стакана или шага цены")
            return

        best_bid = float(order_book.bids[0][0])
        best_ask = float(order_book.asks[0][0])

        current_price = await get_price_from_redis(redis, symbol)
        entry_offset_ticks = (
            Decimal(bot_config.entry_offset_ticks)
            if hasattr(bot_config, "entry_offset_ticks")
            else Decimal(3)
        )
        entry_offset = entry_offset_ticks * tick_size

        # Выбор направления и расчёт цены входа
        if best_ask >= best_bid:
            trade_type = TradeType.BUY
            entry_price = current_price + entry_offset
        else:
            trade_type = TradeType.SELL
            entry_price = current_price - entry_offset

        print(f"⏳ Ждём пробития уровня: {trade_type} по {entry_price:.4f}")

        # Ждём пробоя цены
        while True:
            updated_price = await get_price_from_redis(redis, symbol)
            if trade_type == TradeType.BUY and updated_price >= entry_price:
                break
            elif trade_type == TradeType.SELL and updated_price <= entry_price:
                break
            await asyncio.sleep(0.1)

        # После входа
        current_price = updated_price
        balance = bot_config.balance
        open_commission = balance * COMMISSION_OPEN
        close_commission = balance * COMMISSION_CLOSE

        stop_loss_ticks = Decimal(bot_config.stop_loss_ticks)
        exit_offset_ticks = Decimal(bot_config.exit_offset_ticks)

        stop_loss = (
            current_price - stop_loss_ticks * tick_size
            if trade_type == TradeType.BUY
            else current_price + stop_loss_ticks * tick_size
        )

        if trade_type == TradeType.BUY:
            amount = balance / current_price
            close_price = current_price + exit_offset_ticks * tick_size
            revenue = amount * close_price
            pnl = revenue - balance - open_commission - close_commission
        else:
            amount = balance / current_price
            close_price = current_price - exit_offset_ticks * tick_size
            cost = amount * close_price
            pnl = balance - cost - open_commission - close_commission

        sim_order_crud = TestOrderCrud(session)
        try:
            await sim_order_crud.create(
                {
                    "asset_symbol": symbol,
                    "order_type": trade_type,
                    "balance": balance,
                    "open_price": current_price,
                    "open_time": datetime.now(UTC),
                    "open_fee": open_commission,
                    "stop_loss_price": stop_loss,
                    "bot_id": bot_config.id,
                    "close_price": close_price,
                    "close_time": datetime.now(UTC),
                    "close_fee": close_commission,
                    "profit_loss": pnl,
                    "is_active": False,
                }
            )
            bot_config.total_profit = (
                bot_config.total_profit or Decimal(0)
            ) + pnl

            await session.commit()
            print(
                f"💰 Бот {bot_config.id}: "
                f"общая прибыль {bot_config.total_profit:.4f}"
            )

        except Exception as e:
            print(f"❌ Ошибка при создании ордера: {e}")


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
