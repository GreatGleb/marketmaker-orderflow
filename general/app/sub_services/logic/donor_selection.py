"""Срок публикации донора и отмена ожидания по отозванному выбору."""
import asyncio
import json
import math
import time


DONOR_TTL_SECONDS = 90
CHAIN_REFRESH_SECONDS = 30


def donor_payload(config, now=None):
    return {**config, "published_at": time.time() if now is None else now}


async def read_donor(redis, copybot_id):
    raw = await redis.get(f"copy_bot_{copybot_id}")
    try:
        payload = json.loads(raw) if raw else None
        if not isinstance(payload, dict):
            return None
        published = payload.pop("published_at", None)
        if (isinstance(published, bool) or not isinstance(published, (int, float))
                or not math.isfinite(published)
                or not 0 <= time.time() - published < DONOR_TTL_SECONDS):
            return None
        return payload
    except (TypeError, ValueError):
        return None


class DonorChanged(Exception):
    """Ожидающий бот должен заново выбрать цепочку и параметры входа."""


class DonorGuard:
    def __init__(self, redis, copybot_id, config, chain_check=None):
        self.redis = redis
        self.copybot_id = copybot_id
        self.config = config
        self.chain_check = chain_check
        self.checked_at = time.monotonic()

    async def check(self, final=False):
        # Сначала SQL, потом Redis: отзыв во время запроса тоже запрещает вход.
        if self.chain_check and (
            final or time.monotonic() - self.checked_at >= CHAIN_REFRESH_SECONDS
        ):
            if not await self.chain_check():
                raise DonorChanged
            self.checked_at = time.monotonic()
        if await read_donor(self.redis, self.copybot_id) != self.config:
            raise DonorChanged

    async def wait(self, awaitable):
        """Проверяем выбор и при молчащем источнике цен, и при ожидании MA.

        Продление публикации с тем же конфигом не сбрасывает уровни входа.
        Дочерняя задача всегда завершена/отменена до возврата вызывающему.
        """
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=1)
                await self.check()
                if done:
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
