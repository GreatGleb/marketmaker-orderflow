import asyncio
from app.workers.volatile_pair import VolatilePairCommand


async def main():
    stop_event = asyncio.Event()

    await asyncio.gather(
        VolatilePairCommand(stop_event=stop_event).run_async(),
    )


if __name__ == "__main__":
    asyncio.run(main())
