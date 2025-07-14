import asyncio
import json
from datetime import datetime

from fastapi import Depends

from redis.asyncio import Redis

from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.order import ORDER_QUEUE_KEY
from app.crud.test_orders import TestOrderCrud
from app.dependencies import get_session, resolve_crud, get_redis
from app.utils import Command, CommandResult


class OrderBulkInsert(Command):

    @staticmethod
    def parse_datetime_fields(order, datetime_fields: list[str]) -> dict:
        for field in datetime_fields:
            if field in order and isinstance(order[field], str):
                order[field] = datetime.fromisoformat(order[field])
        return order

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        crud: TestOrderCrud = resolve_crud(TestOrderCrud),
        redis: Redis = Depends(get_redis),
    ) -> CommandResult:

        orders = []
        DATETIME_FIELDS = ["open_time", "close_time"]

        for _ in range(1000):
            raw = await redis.lpop(ORDER_QUEUE_KEY)
            if raw is None:
                break

            order = json.loads(raw)
            order = self.parse_datetime_fields(order, DATETIME_FIELDS)
            orders.append(order)

        if not orders:
            return CommandResult(success=True)

        await crud.bulk_create(orders=orders)

        return CommandResult(success=True)


async def main() -> None:
    await OrderBulkInsert().run_async()


if __name__ == "__main__":
    print("🧹 Starting OrderBulkInsert...")
    asyncio.run(main())
    print("✅ OrderBulkInsert finished.")
