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

        trade_type = None
        if current_price >= best_ask:
            trade_type = TradeType.BUY
        elif current_price <= best_bid:
            trade_type = TradeType.SELL

        if not trade_type:
            await asyncio.sleep(0.1)
            continue

        # Вычисляем цену входа и стоп откат
        entry_offset = bot_config.start_ticks * tick_size
        stop_entry_offset = bot_config.stop_ticks * tick_size

        entry_price = (
            current_price + entry_offset
            if trade_type == TradeType.BUY
            else current_price - entry_offset
        )
        cancel_entry_price = (
            current_price - stop_entry_offset
            if trade_type == TradeType.BUY
            else current_price + stop_entry_offset
        )

        print(
            f"⏳ Бот {bot_config.id} | {trade_type} "
            f"| Текущая: {current_price:.4f} | Ждём: {entry_price:.4f}"
        )

        # Ждём входа в позицию
        while True:
            updated_price = await get_price_from_redis(redis, symbol)

            if trade_type == TradeType.BUY:
                if updated_price >= entry_price:
                    break
                if updated_price <= cancel_entry_price:
                    print(
                        f"❌ Бот {bot_config.id} | BUY | "
                        f"Вход отменён, цена ушла ниже допустимого порога"
                    )
                    await asyncio.sleep(0.1)
                    continue
            else:
                if updated_price <= entry_price:
                    break
                if updated_price >= cancel_entry_price:
                    print(
                        f"❌ Бот {bot_config.id} | SELL | "
                        f"Вход отменён, цена ушла выше допустимого порога"
                    )
                    await asyncio.sleep(0.1)
                    continue

            await asyncio.sleep(0.1)

        # Вход выполнен
        open_price = updated_price
        stop_loss_price = (
            open_price - bot_config.stop_loss_ticks * tick_size
            if trade_type == TradeType.BUY
            else open_price + bot_config.stop_loss_ticks * tick_size
        )
        take_profit_price = (
            open_price + bot_config.take_profit_ticks * tick_size
            if trade_type == TradeType.BUY
            else open_price - bot_config.take_profit_ticks * tick_size
        )

        print(
            f"🔎 Бот {bot_config.id} | {trade_type} "
            f"| Вход по: {open_price:.4f}, SL: {stop_loss_price:.4f}, "
            f"TP: {take_profit_price:.4f}"
        )

        order = TestOrder(
            stop_loss_price=Decimal(stop_loss_price),
            open_price=open_price,
            open_time=datetime.now(UTC),
            open_fee=Decimal(open_price) * Decimal(COMMISSION_OPEN),
        )

        while True:
            updated_price = await get_price_from_redis(redis, symbol)

            if trade_type == TradeType.BUY:
                if updated_price >= take_profit_price:
                    break
                if updated_price <= order.stop_loss_price:
                    break
                # Прибыльная зона — подтягиваем SL
                if updated_price - open_price >= tick_size:
                    new_sl = (
                        updated_price
                        - bot_config.successful_stop_lose_ticks * tick_size
                    )
                    if new_sl > float(order.stop_loss_price):
                        order.stop_loss_price = Decimal(new_sl)
            else:
                if updated_price <= take_profit_price:
                    break
                if updated_price >= order.stop_loss_price:
                    break
                # Прибыльная зона — подтягиваем SL
                if open_price - updated_price >= tick_size:
                    new_sl = (
                        updated_price
                        + bot_config.successful_stop_lose_ticks * tick_size
                    )
                    if new_sl < float(order.stop_loss_price):
                        order.stop_loss_price = Decimal(new_sl)

            await asyncio.sleep(0.1)

        # Закрытие сделки
        close_price = await get_price_from_redis(redis, symbol)
        balance = bot_config.balance
        amount = Decimal(balance) / Decimal(open_price)
        revenue = amount * Decimal(close_price)
        total_commission = Decimal(balance) * Decimal(
            COMMISSION_OPEN + COMMISSION_CLOSE
        )
        pnl = (
            revenue - Decimal(balance) - total_commission
            if trade_type == TradeType.BUY
            else Decimal(balance)
            - (amount * Decimal(close_price))
            - total_commission
        )

        print(
            f"💬 Бот {bot_config.id} | {trade_type} | "
            f"Entry: {open_price:.4f} | Close: {close_price:.4f} | "
            f"Amount: {amount:.6f} | PnL: {pnl:.4f} | "
            f"TP_ticks: {bot_config.take_profit_ticks} | "
            f"SL_ticks: {bot_config.stop_loss_ticks} | "
            f"Start_ticks: {bot_config.start_ticks} | "
            f"Stop_ticks: {bot_config.stop_ticks}"
        )

        try:
            successful_stop_lose_ticks = (
                bot_config.successful_stop_lose_ticks,
            )
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
                    "start_ticks": bot_config.start_ticks,
                    "stop_ticks": bot_config.stop_ticks,
                    "stop_loss_ticks": bot_config.stop_loss_ticks,
                    "successful_stop_lose_ticks": successful_stop_lose_ticks,
                }
            )
            await session.commit()

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
                        # try:
                        await simulate_bot(
                            session=session,
                            bot_config=bot_config,
                            shared_data=shared_data,
                            redis=redis,
                        )
                        # except Exception as e:
                        #     print(f"❌ Ошибка в боте {bot_config.id}: {e}")
                        #     await asyncio.sleep(1)

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
