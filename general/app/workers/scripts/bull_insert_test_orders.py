import asyncio
from app.workers.bulk_insert_orders import OrderBulkInsertCommand

async def main():
    await asyncio.gather(
        OrderBulkInsertCommand().run_async(),
    )

    print("✅ Все заказы завершены.")


if __name__ == "__main__":
    asyncio.run(main())
