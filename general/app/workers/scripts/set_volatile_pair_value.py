import argparse
import asyncio
from app.workers.volatile_pair import VolatilePairCommand


async def main(is_need_list_of_symbols=False):
    stop_event = asyncio.Event()

    await asyncio.gather(
        VolatilePairCommand(stop_event=stop_event, is_need_list_of_symbols=is_need_list_of_symbols).run_async(),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Обновляет статистику")
    parser.add_argument('-l', '--list', type=int, help="List")
    args = parser.parse_args()

    if args.list:
        asyncio.run(main(is_need_list_of_symbols=args.list))
    else:
        asyncio.run(main())
