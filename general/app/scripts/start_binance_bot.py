import asyncio
import threading
import time

from app.bots.binance_bot import BinanceBot

def input_listener(loop, stop_event):
    while not stop_event.is_set():
        cmd = (
            input("👉 Введите 'stop' чтобы остановить бота:\n").strip().lower()
        )
        if cmd == "stop":
            print("🛑 Останавливаем бота...")
            loop.call_soon_threadsafe(stop_event.set)
            break

        time.sleep(1)


async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    input_thread = threading.Thread(
        target=input_listener, args=(loop, stop_event)
    )
    input_thread.start()

    await asyncio.gather(
        BinanceBot(stop_event=stop_event).run_async(),
    )

    print("✅ Бот завершён.")
    stop_event.set()
    input_thread.join()


if __name__ == "__main__":
    asyncio.run(main())
