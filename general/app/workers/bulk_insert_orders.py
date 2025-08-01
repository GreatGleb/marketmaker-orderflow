import asyncio
import json
import logging
import time

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Depends

from redis.asyncio import Redis

from app.config import settings
from app.constants.order import ORDER_QUEUE_KEY
from app.crud.test_orders import TestOrderCrud
from app.db.base import DatabaseSessionManager
from app.dependencies import (
    get_redis,
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
        redis: Redis = Depends(get_redis),
    ):
        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        dsm = DatabaseSessionManager.create(settings.DB_URL)

        DATETIME_FIELDS = [
            "open_time",
            "close_time",
            "created_at",
            "updated_at",
        ]
        BATCH_SIZE = 1000

        while True:
            start_time = time.time()

            async with dsm.get_session() as session:
                test_order_crud = TestOrderCrud(session)
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
                        logging.info(f"❌ Ошибка при обработке записи из Redis: {e}")

                RETRY_DELAY_SECONDS = 5 * 60
                EMPTY_ORDERS_DELAY_SECONDS = 1 * 60

                for i in range(0, len(orders), BATCH_SIZE):
                    batch = orders[i : i + BATCH_SIZE]
                    try:
                        await test_order_crud.bulk_create(orders=batch)
                    except Exception as e:
                        logging.info(f"❌ Ошибка при вставке батча в БД: {e}")
                        logging.info("Ждем {RETRY_DELAY_SECONDS // 60} мин. и пробуем снова.")
                        await asyncio.sleep(RETRY_DELAY_SECONDS)
                        try:
                            await test_order_crud.bulk_create(orders=batch)
                        except Exception as e_retry:
                            logging.info(f"❌ Повторная попытка тоже не удалась: {e_retry}. Пропускаем батч.")

                if not orders:
                    logging.info(f"Список заказов пуст. Ждем {EMPTY_ORDERS_DELAY_SECONDS // 60} минуту...")
                    await asyncio.sleep(EMPTY_ORDERS_DELAY_SECONDS)

            end_time = time.time()
            elapsed_time = end_time - start_time
            wait_time = 60 - elapsed_time
            await asyncio.sleep(wait_time)

        return CommandResult(success=True)
