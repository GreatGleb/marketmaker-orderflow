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

# Сделки забираются из Redis через LPOP, то есть удаляются до вставки в БД.
# При сбое БД батч возвращается в очередь — но только пока она не разрослась:
# одна сделка занимает ~600 байт, без потолка длинный простой БД съест всю
# память Redis (maxmemory в docker-compose не задан).
# 200 000 записей — примерно 125 МБ.
QUEUE_MAX_LENGTH = 200_000


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
                for iteration in range(10):
                    orders = []
                    raws = []
                    for _ in range(4000):
                        raw = await redis.lpop(ORDER_QUEUE_KEY)

                        if raw is None:
                            break

                        try:
                            order = json.loads(raw)
                            order = self.parse_datetime_fields(order, DATETIME_FIELDS)
                        except Exception as e:
                            logging.info(f"❌ Ошибка при обработке записи из Redis: {e}")
                            continue

                        orders.append(order)
                        raws.append(raw)

                    EMPTY_ORDERS_DELAY_SECONDS = 1 * 60

                    for i in range(0, len(orders), BATCH_SIZE):
                        batch = orders[i : i + BATCH_SIZE]
                        try:
                            await test_order_crud.bulk_create(orders=batch)
                        except Exception as e:
                            logging.info(f"❌ Ошибка при вставке батча в БД: {e}")

                            # Иначе транзакция остаётся сломанной и следующие
                            # батчи падают уже из-за неё.
                            await session.rollback()

                            batch_raws = raws[i : i + BATCH_SIZE]
                            queue_length = await redis.llen(ORDER_QUEUE_KEY)

                            if queue_length + len(batch_raws) <= QUEUE_MAX_LENGTH:
                                await redis.lpush(ORDER_QUEUE_KEY, *batch_raws)
                                logging.info(
                                    f"↩️ Батч из {len(batch_raws)} сделок возвращён "
                                    f"в очередь, повтор на следующем витке."
                                )
                            else:
                                logging.info(
                                    f"⚠️ Батч из {len(batch_raws)} сделок не влезает "
                                    f"в лимит очереди ({queue_length} + "
                                    f"{len(batch_raws)} > {QUEUE_MAX_LENGTH}) и потерян. "
                                    f"Починить БД или поднять QUEUE_MAX_LENGTH."
                                )

                    if not orders:
                        logging.info(f"Список заказов пуст. Ждем {EMPTY_ORDERS_DELAY_SECONDS // 60} минуту...")
                        await asyncio.sleep(EMPTY_ORDERS_DELAY_SECONDS)

            end_time = time.time()
            elapsed_time = end_time - start_time
            wait_time = 60 - elapsed_time
            await asyncio.sleep(max(wait_time, 1))

        return CommandResult(success=True)
