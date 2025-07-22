import asyncio
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand


async def main():
    stop_event = asyncio.Event()

    await asyncio.gather(
        ProfitableBotUpdaterCommand(stop_event=stop_event).run_async(),
    )


if __name__ == "__main__":
    asyncio.run(main())
