import asyncio
import json

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.constants.order import ORDER_QUEUE_KEY
from app.crud.test_orders import TestOrderCrud
from app.dependencies import (
    get_session,
    get_redis,
    resolve_crud,
)

from app.utils import Command, CommandResult


class OrderBulkInsertCommand(Command):

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
    ):
        DATETIME_FIELDS = [
            "open_time",
            "close_time",
            "created_at",
            "updated_at",
        ]
        BATCH_SIZE = 1000

        orders = []
        for _ in range(4000):
            raw = await redis.lpop(ORDER_QUEUE_KEY)

            if raw is None:
                break

            try:
                order = json.loads(raw)
                order = self.parse_datetime_fields(order, DATETIME_FIELDS)
                orders.append(order)
            except Exception as e:
                print(f"❌ Ошибка при обработке записи из Redis: {e}")

        RETRY_DELAY_SECONDS = 5 * 60
        EMPTY_ORDERS_DELAY_SECONDS = 1 * 60

        for i in range(0, len(orders), BATCH_SIZE):
            batch = orders[i : i + BATCH_SIZE]
            try:
                await crud.bulk_create(orders=batch)
            except Exception as e:
                print(f"❌ Ошибка при вставке батча в БД: {e}")
                print("Ждем {RETRY_DELAY_SECONDS // 60} мин. и пробуем снова.")
                await asyncio.sleep(RETRY_DELAY_SECONDS)
                try:
                    await crud.bulk_create(orders=batch)
                except Exception as e_retry:
                    print(f"❌ Повторная попытка тоже не удалась: {e_retry}. Пропускаем батч.")

        if not orders:
            print(f"Список заказов пуст. Ждем {EMPTY_ORDERS_DELAY_SECONDS // 60} минуту...")
            await asyncio.sleep(EMPTY_ORDERS_DELAY_SECONDS)

        return CommandResult(success=True)
