"""Настоящий Redis: повтор живого инцидента и атомарный срок жизни цены.

PRICE_TEST_REDIS_URL должен указывать на отдельный тестовый Redis.
Проверка использует уникальные ключи и удаляет только их.
"""
import asyncio
import json
import os
import uuid
from unittest.mock import AsyncMock, patch

from redis.asyncio import Redis
from app.sub_services.watchers.price_snapshot import publish_prices
from app.scripts.watch_ws_and_save import save_filtered_assets


async def main():
    url = os.getenv('PRICE_TEST_REDIS_URL')
    if not url:
        print('Пропущено: нужен PRICE_TEST_REDIS_URL отдельного Redis')
        return
    redis = Redis.from_url(url, decode_responses=True)
    symbol = 'PRICE_TEST_' + uuid.uuid4().hex
    keys = [f'price_snapshot:{symbol}', f'price:{symbol}']
    seconds, micros = await redis.time()
    now = seconds * 1000 + micros // 1000

    def tick(price, event):
        return {'symbol': symbol, 'price': price, 'event_time_ms': event, 'source': 'TEST'}

    try:
        # Реальная последовательность BMT: свежая -> пятичасовая -> свежая.
        assert await publish_prices(redis, [
            tick('0.01848', now - 10_000),
            tick('0.0192', now - 5 * 3600_000),
            tick('0.01847', now - 3_000),
        ]) == [True, False, True]
        assert await redis.get(keys[1]) == '0.01847'
        expiry = await redis.pexpiretime(keys[0])
        assert expiry == now - 3_000 + 120_000
        # Новое соединение имитирует перезапуск питателя: метка не теряется.
        async with Redis.from_url(url, decode_responses=True) as restarted:
            assert await publish_prices(restarted, [
                tick('0.0192', now - 4_000),
                tick('0.0192', now - 3_000),
                tick('0.0192', now + 30_000),
            ]) == [False, False, False]
        assert await redis.pexpiretime(keys[0]) == expiry
        assert await redis.pexpiretime(keys[1]) == expiry
        assert json.loads(await redis.get(keys[0]))['price'] == '0.01847'
        # Два одновременных издателя не могут откатить время.
        await asyncio.gather(
            publish_prices(redis, [tick('0.01846', now - 2_000)]),
            publish_prices(redis, [tick('0.01845', now - 1_000)]),
        )
        assert await redis.get(keys[1]) == '0.01845'
        # Сквозной путь питателя: в историю тоже попадают только принятые.
        history = AsyncMock()
        watched = AsyncMock()
        watched.get_symbol_to_id_map.return_value = {symbol: 1}
        session = AsyncMock()
        with patch('app.scripts.watch_ws_and_save.AssetHistoryCrud', return_value=history), \
             patch('app.scripts.watch_ws_and_save.WatchedPairCrud', return_value=watched):
            await save_filtered_assets(session, redis, [
                {'s': symbol, 'c': '0.0192', 'E': now - 5 * 3600_000},
                {'s': symbol, 'c': '0.0192'},
                {'s': symbol, 'c': 'NaN', 'E': now},
                {'s': symbol, 'c': '0.01844', 'E': now},
            ], True)
            records = history.bulk_create.call_args.args[0]
            assert len(records) == 1 and records[0]['last_price'] == '0.01844'
            session.commit.assert_awaited_once()
            history.bulk_create.reset_mock()
            await save_filtered_assets(session, redis, [
                {'s': symbol, 'c': '0.0192', 'E': now - 500},
            ], True)
            history.bulk_create.assert_not_awaited()
            session.rollback.assert_awaited_once()
        print('OK: пятичасовой ответ, повторы, откат, перезапуск, гонка и срок от события')
    finally:
        await redis.delete(*keys)
        await redis.aclose()


if __name__ == '__main__':
    asyncio.run(main())
