import asyncio
import threading

from datetime import timezone

from app.bots.demo_test_bot import StartTestBotsCommand

# from app.workers.bulk_insert_orders import OrderBulkInsertCommand
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand
from app.workers.volatile_pair import VolatilePairCommand

UTC = timezone.utc


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
        VolatilePairCommand(stop_event=stop_event).run_async(),
        # ProfitableBotUpdaterCommand(stop_event=stop_event).run_async(),
        StartTestBotsCommand(stop_event=stop_event).run_async(),
        # OrderBulkInsertCommand(stop_event=stop_event).run_async(),
    )

    print("✅ Все боты завершены.")


if __name__ == "__main__":
    asyncio.run(main())
