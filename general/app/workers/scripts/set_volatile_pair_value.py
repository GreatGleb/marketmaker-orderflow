import asyncio
from app.workers.volatile_pair import VolatilePairCommand


async def main(is_need_list_of_symbols=False):
    stop_event = asyncio.Event()

    await asyncio.gather(
        VolatilePairCommand(stop_event=stop_event, is_need_list_of_symbols=is_need_list_of_symbols).run_async(),
    )


if __name__ == "__main__":
    asyncio.run(main(is_need_list_of_symbols=True))
