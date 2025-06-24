import asyncio
import enum
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.crud.asset_history import AssetHistoryCrud
from app.crud.asset_order_book import AssetOrderBookCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.crud.test_bot import TestBotCrud
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestBot

UTC = timezone.utc

COMMISSION_OPEN = Decimal(0.0002)  # 0.02%
COMMISSION_CLOSE = Decimal(0.0005)  # 0.05%


class TradeType(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


async def simulate_bot(session, bot_config: TestBot):
    now = datetime.now(UTC)
    five_minutes_ago = now - timedelta(minutes=5)

    # 1. Найти наиболее волатильную пару
    asset_crud = AssetHistoryCrud(session)
    most_volatile = await asset_crud.get_most_volatile_since(
        since=five_minutes_ago
    )

    if not most_volatile:
        print("Нет волатильных пар")
        return

    symbol = bot_config.symbol
    current_price = most_volatile.last_price

    # 2. Получить актуальный ордербук
    order_book_crud = AssetOrderBookCrud(session)
    order_book = await order_book_crud.get_latest_by_symbol(symbol)

    if not order_book or not order_book.bids or not order_book.asks:
        print("Нет данных в order book")
        return

    best_bid = float(order_book.bids[0][0])  # noqa
    best_ask = float(order_book.asks[0][0])  # noqa

    # 3. Выбрать направление торговли
    if current_price >= best_ask:
        trade_type = TradeType.BUY
    elif current_price <= best_bid:
        trade_type = TradeType.SELL
    else:
        print("Цена не достигла ближайших bid/ask")
        return

    # 4. Получить шаг цены
    exchange_crud = AssetExchangeSpecCrud(session)
    step_sizes = await exchange_crud.get_step_size_by_symbol(symbol)

    if not step_sizes or not step_sizes["tick_size"]:
        print("❌ Не найден шаг цены (tick_size)")
        return

    tick_size = Decimal(str(step_sizes["tick_size"]))
    min_move_size = Decimal(bot_config.stop_loss_ticks) * tick_size

    stop_loss = (
        current_price - min_move_size
        if trade_type == TradeType.BUY
        else current_price + min_move_size
    )

    balance = bot_config.balance

    # 6. Рассчитать комиссию за открытие
    open_commission = balance * COMMISSION_OPEN

    # 7. Создать ордер
    sim_order_crud = TestOrderCrud(session)
    order = await sim_order_crud.create(
        {
            "asset_symbol": symbol,
            "order_type": trade_type,
            "balance": Decimal(balance),
            "open_price": Decimal(current_price),
            "open_time": now,
            "open_fee": Decimal(current_price) * COMMISSION_OPEN,
            "stop_loss_price": Decimal(stop_loss),
            "bot_id": bot_config.id,
        }
    )
    print(
        f"🟢 Открыт ордер: {order.asset_symbol} {order.order_type} "
        f"по цене {order.open_price} с балансом {order.balance}"
    )

    # 8. Следим за ценой и поднимаем stop-loss
    while True:
        await asyncio.sleep(0.1)  # можно увеличить интервал
        updated_price = await asset_crud.get_latest_price(symbol)
        if updated_price is None:
            continue

        stop_loss_step = Decimal(bot_config.stop_loss_ticks) * tick_size
        stop_loss = (
            current_price - stop_loss_step
            if trade_type == TradeType.BUY
            else current_price + stop_loss_step
        )

        exit_step = Decimal(bot_config.exit_offset_ticks) * tick_size
        last_profit_price = (
            order.stop_loss_price + exit_step
            if trade_type == TradeType.BUY
            else order.stop_loss_price - exit_step
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

    # 9. Закрытие ордера
    close_time = datetime.now(UTC)
    close_price = await asset_crud.get_latest_price(symbol)
    close_commission = balance * COMMISSION_CLOSE

    if trade_type == TradeType.BUY:
        amount = Decimal(balance) / Decimal(order.open_price)

        revenue = amount * Decimal(close_price)

        total_commission = Decimal(open_commission) + Decimal(close_commission)

        pnl = revenue - Decimal(balance) - total_commission

    else:
        total_commission = Decimal(open_commission) + Decimal(close_commission)

        amount = Decimal(balance) / Decimal(order.open_price)

        cost = amount * Decimal(close_price)

        pnl = Decimal(balance) - cost - total_commission

    await sim_order_crud.close_order(
        order_id=order.id,
        close_price=close_price,
        close_time=close_time,
        close_fee=order.open_price * COMMISSION_OPEN,
        profit_loss=pnl,
    )

    print(f"🔴 Ордер закрыт. PnL: {pnl:.4f}, цена закрытия: {close_price:.4f}")


is_bot_running = True


# async def simulate_bot_loop():
#     global is_bot_running  # noqa
#
#     dsm = DatabaseSessionManager.create(settings.DB_URL)
#
#     while is_bot_running:
#         async with dsm.get_session() as session:
#             await simulate_bot(session=session)
#
#         await asyncio.sleep(5)


async def simulate_multiple_bots():
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)
        bots = await bot_crud.get_active_bots()

    tasks = []

    for bot in bots:

        async def _run_bot(bot_config=bot):
            async with dsm.get_session() as session:
                await simulate_bot(session=session, bot_config=bot_config)

        tasks.append(asyncio.create_task(_run_bot()))

    await asyncio.gather(*tasks)


def input_listener():

    global is_bot_running

    while True:
        cmd = (
            input("👉 Введите 'stop' чтобы остановить бота:\n").strip().lower()
        )
        if cmd == "stop":
            print("🛑 Останавливаем бота...")
            is_bot_running = False
            break


def main():
    # Запуск input в отдельном потоке
    input_thread = threading.Thread(target=input_listener)
    input_thread.start()

    # Запуск бота
    asyncio.run(simulate_multiple_bots())

    print("✅ Бот полностью завершён.")


if __name__ == "__main__":
    main()
