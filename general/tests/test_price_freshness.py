"""Просроченная цена недоступна даже при зависшем обновлении кэша."""
import asyncio
from contextlib import suppress
from decimal import Decimal
from unittest.mock import patch

from app.scripts.watch_ws_and_save import _rest_tickers_to_ws_format
from app.sub_services.watchers.price_provider import PriceCache, PriceProvider
from app.sub_services.watchers.price_snapshot import parse_snapshot
from tests.price_fixtures import price_snapshot


class Redis:
    def __init__(self, store):
        self.store = store

    async def mget(self, keys):
        return [self.store.get(key) for key in keys]

    async def get(self, key):
        return self.store.get(key)


async def main():
    now = 1_800_000_000_000
    assert parse_snapshot(price_snapshot('0.01848', now - 3_000), now)
    for value in ('0', '-1', 'NaN', 'Infinity', 'bad'):
        assert parse_snapshot(price_snapshot(value, now), now) is None
    for event in (now - 120_000, now - 5 * 3600_000, now + 5_001, None, '1', True):
        assert parse_snapshot({'price': '0.0192', 'event_time_ms': event}, now) is None
    for value in (None, '0.0192', '{}', '[]', 'null', '{'):
        assert parse_snapshot(value, now) is None
    assert _rest_tickers_to_ws_format([{'symbol': 'BMTUSDT', 'lastPrice': '0.0192'}])[0]['E'] is None

    redis = Redis({'price:BMTUSDT': '0.0192'})
    assert await PriceProvider(redis)._read_price('BMTUSDT') is None
    redis.store['price_snapshot:BMTUSDT'] = price_snapshot('0.01848', now - 119_000)
    redis.store['price_snapshot:BAD'] = 'broken'
    cache = PriceCache(redis)
    cache.track('BMTUSDT')
    cache.track('BAD')
    with patch('time.time', return_value=now / 1000), patch('time.monotonic', return_value=10):
        await cache._refresh_once()
        assert cache.get('BMTUSDT') == Decimal('0.01848')
        assert cache.get('BAD') is None
        assert await PriceProvider(redis)._read_price('BMTUSDT') == Decimal('0.01848')
    with patch('time.monotonic', return_value=11):
        assert cache.get('BMTUSDT') is None, 'Зависший MGET не должен замораживать цену'
    with patch('time.time', return_value=now / 1000 - 60), patch('time.monotonic', return_value=12):
        await cache._refresh_once()
        assert cache.get('BMTUSDT') is None, 'Перевод часов назад не продлевает цену'
    with patch('time.time', return_value=now / 1000 + 2):
        assert await PriceProvider(redis)._read_price('BMTUSDT') is None

    class FailingRedis(Redis):
        fail = False
        failed = asyncio.Event()

        async def mget(self, keys):
            if self.fail:
                self.failed.set()
                raise ConnectionError('Проверочный отказ Redis')
            return await super().mget(keys)

    flaky = FailingRedis({'price_snapshot:BMTUSDT': price_snapshot('0.01847')})
    cache = PriceCache(flaky)
    cache.track('BMTUSDT')
    await cache._refresh_once()
    assert cache.get('BMTUSDT') == Decimal('0.01847')
    cache.ERROR_RETRY_SECONDS = 0.01
    flaky.fail = True
    cache.start()
    try:
        await asyncio.wait_for(flaky.failed.wait(), 1)
        assert cache.get('BMTUSDT') is None, 'Ошибка Redis должна сбрасывать имеющуюся цену'
        flaky.fail = False
        assert await asyncio.wait_for(PriceProvider(flaky, cache).get_price('BMTUSDT'), 1) == Decimal('0.01847')
    finally:
        cache._task.cancel()
        with suppress(asyncio.CancelledError):
            await cache._task
    print('OK: возраст, неверные данные, отсутствие времени, зависший кэш и отказ Redis')


if __name__ == '__main__':
    asyncio.run(main())
