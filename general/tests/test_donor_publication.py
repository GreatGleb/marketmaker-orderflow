"""Реальный Redis: TTL, истечение и отзыв выбора циклом воркера.

Нужен DONOR_TEST_REDIS_URL отдельного тестового Redis; используются только
уникальные ключи, которые удаляются в finally.
"""
import asyncio
import json
import os
import time
import uuid
from unittest.mock import AsyncMock, patch

from redis.asyncio import Redis

import app.workers.profitable_bot_updater as worker
from app.sub_services.logic.donor_selection import (
    DonorChanged, DonorGuard, donor_payload, read_donor,
)
from tests.test_copybot_v3_simulation import DONOR_CONFIG, FakeSessionManager
from tests.test_worker_session_release import FakeCrud


async def main():
    url = os.getenv('DONOR_TEST_REDIS_URL')
    if not url:
        print('Пропущено: нужен DONOR_TEST_REDIS_URL отдельного Redis')
        return
    bot_id = uuid.uuid4().int
    key = f'copy_bot_{bot_id}'
    redis = Redis.from_url(url, decode_responses=True)
    stop = asyncio.Event()

    class Crud(FakeCrud):
        def __init__(self, session):
            super().__init__(session)
            self.copybot.id = bot_id

    cycles = 0

    async def observe(seconds):
        nonlocal cycles
        cycles += 1
        if cycles == 1:
            assert 0 < await redis.pttl(key) <= 90000
            assert await read_donor(redis, bot_id) == DONOR_CONFIG
            guard = DonorGuard(redis, bot_id, DONOR_CONFIG)
            await guard.check()
            # Проверяем настоящее истечение Redis без ожидания полутора минут.
            await redis.pexpire(key, 1)
            for _ in range(100):
                if not await redis.exists(key):
                    break
                await asyncio.sleep(.01)
            assert not await redis.exists(key)
            try:
                await guard.check()
                assert False, 'истёкший ключ сохранил право входа'
            except DonorChanged:
                pass
            await redis.set(key, json.dumps(donor_payload(DONOR_CONFIG, time.time() - 91)), ex=90)
            assert await read_donor(redis, bot_id) is None
            await redis.set(key, json.dumps(donor_payload(DONOR_CONFIG)), ex=90)
        else:
            # Второй цикл не нашёл кандидата и удалил ещё живой ключ.
            assert not await redis.exists(key)
            stop.set()

    real_sleep = asyncio.sleep

    async def worker_sleep(seconds):
        if seconds == 30:
            await observe(seconds)
        else:
            await real_sleep(seconds)

    cmd = worker.ProfitableBotUpdaterCommand(stop_event=stop)
    try:
        with patch.object(worker, 'DatabaseSessionManager', FakeSessionManager), \
                patch.object(worker, 'TestBotCrud', Crud), \
                patch.object(cmd, 'get_bot_config_by_params', AsyncMock(side_effect=[DONOR_CONFIG, None])), \
                patch.object(worker.asyncio, 'sleep', worker_sleep):
            await cmd.command(redis)
        assert cycles == 2
        print('  настоящий Redis: TTL 90 с, истечение, возраст публикации и удаление живого выбора')
    finally:
        await redis.delete(key)
        await redis.aclose()


if __name__ == '__main__':
    asyncio.run(main())
