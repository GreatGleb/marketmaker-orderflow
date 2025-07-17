import asyncio
import threading

from app.bots.binance_bot import BinanceBot

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
        BinanceBot(stop_event=stop_event).run_async(),
    )

    print("✅ Бот завершён.")


if __name__ == "__main__":
    asyncio.run(main())
