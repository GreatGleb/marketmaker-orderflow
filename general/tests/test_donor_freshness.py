"""Отзыв, истечение и замена уже загруженного донора до входа."""
import asyncio
import json
import time
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import app.bots.demo_test_bot as m
import app.workers.profitable_bot_updater as worker
from app.sub_services.logic.donor_selection import (
    DonorChanged, DonorGuard, donor_payload, read_donor,
)
from tests.test_copybot_v3_simulation import (
    COPYBOT_V1_ID, DONOR_CONFIG, DONOR_ID, FakeCrud, FakeRedis,
    FakeSessionManager, MARKET, PRICE, SYMBOL, V3_BOT,
)
from tests.test_worker_session_release import FakeCrud as WorkerCrud
from tests.price_fixtures import price_snapshot

KEY = f'copy_bot_{COPYBOT_V1_ID}'


async def check_publication():
    redis = FakeRedis()
    now = time.time()
    for value in (None, '{}', 'broken', '[]', json.dumps(DONOR_CONFIG),
                  json.dumps(donor_payload(DONOR_CONFIG, now - 91)),
                  json.dumps(donor_payload(DONOR_CONFIG, now + 10)),
                  json.dumps(donor_payload(DONOR_CONFIG, float('nan')))):
        redis.store[KEY] = value
        assert await read_donor(redis, COPYBOT_V1_ID) is None
    redis.store[KEY] = json.dumps(donor_payload(DONOR_CONFIG))
    assert await read_donor(redis, COPYBOT_V1_ID) == DONOR_CONFIG

    # Реальный цикл воркера: первый выбор публикуется с TTL, второй отзывается.
    redis = AsyncMock()
    stop = asyncio.Event()
    calls = 0

    async def next_cycle(seconds):
        nonlocal calls
        calls += 1
        if calls == 2:
            stop.set()

    cmd = worker.ProfitableBotUpdaterCommand(stop_event=stop)
    with patch.object(worker, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(worker, 'TestBotCrud', WorkerCrud), \
            patch.object(cmd, 'get_bot_config_by_params', AsyncMock(side_effect=[DONOR_CONFIG, None])), \
            patch.object(worker.asyncio, 'sleep', next_cycle):
        await cmd.command(redis)
    redis.set.assert_awaited_once()
    args, kwargs = redis.set.await_args
    assert args[0] == 'copy_bot_7' and kwargs == {'ex': 90}
    assert 0 <= time.time() - json.loads(args[1])['published_at'] < 5
    redis.delete.assert_awaited_once_with('copy_bot_7')
    print('  старые/неверные публикации запрещены, воркер отзывает выбор и задаёт TTL')


async def check_wait_cancellation():
    redis = FakeRedis()
    guard = DonorGuard(redis, COPYBOT_V1_ID, DONOR_CONFIG)
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def missing_price():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(guard.wait(missing_price()))
    await started.wait()
    redis.store.pop(KEY)
    try:
        await asyncio.wait_for(task, 3)
        assert False, 'ожидание старого донора продолжилось'
    except DonorChanged:
        pass
    assert cancelled.is_set()

    # Внешняя отмена симулятора тоже должна дождаться дочерней задачи.
    redis.store[KEY] = json.dumps(donor_payload(DONOR_CONFIG))
    started.clear()
    cancelled.clear()
    task = asyncio.create_task(guard.wait(missing_price()))
    await started.wait()
    task.cancel()
    try:
        await task
        assert False
    except asyncio.CancelledError:
        pass
    assert cancelled.is_set()

    # Ошибка Redis запрещает вход и не оставляет ожидание работать в фоне.
    cancelled.clear()
    broken_redis = AsyncMock()
    broken_redis.get.side_effect = ConnectionError('Redis недоступен')
    try:
        await DonorGuard(broken_redis, COPYBOT_V1_ID, DONOR_CONFIG).wait(missing_price())
        assert False
    except ConnectionError:
        pass
    assert cancelled.is_set()

    # Истечение метаданных тоже запрещает вход, даже если TTL Redis не отработал.
    redis.store[KEY] = json.dumps(donor_payload(DONOR_CONFIG, time.time() - 91))
    try:
        await guard.check()
        assert False
    except DonorChanged:
        pass
    # Тот же донор с новой публикацией сохраняет начатое ожидание.
    redis.store[KEY] = json.dumps(donor_payload(DONOR_CONFIG))
    assert await guard.wait(asyncio.sleep(0, result=42)) == 42

    chain = AsyncMock(return_value=False)
    guard = DonorGuard(redis, COPYBOT_V1_ID, DONOR_CONFIG, chain)
    guard.checked_at -= 31
    try:
        await guard.wait(missing_price())
        assert False
    except DonorChanged:
        pass
    chain.assert_awaited_once()
    print('  ожидание без цены отменяется; обновление того же конфига его сохраняет')


async def attempt(redis, bot, signal, *, provider=None, crud=FakeCrud):
    stop = asyncio.Event()
    command = m.StartTestBotsCommand(stop_event=stop)
    if provider is None:
        provider = AsyncMock()
        provider.get_price.return_value = PRICE
        provider._read_price.return_value = PRICE

    async def wait(*args, **kwargs):
        return await signal(stop, kwargs)

    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', crud), \
            patch.object(m.PriceWatcher, 'wait_for_entry_price', wait):
        await command.simulate_bot(redis, bot, {SYMBOL: dict(MARKET)}, stop, provider, None)
    return command


async def check_simulator_revocation():
    v1 = V3_BOT._replace(id=COPYBOT_V1_ID, copybot_v3_time_in_minutes=None,
                         copybot_v3_compound_balance=False, copy_bot_min_time_profitability_min=Decimal(30))
    v2 = V3_BOT._replace(copybot_v3_time_in_minutes=None, copybot_v3_compound_balance=False,
                         copybot_v2_time_in_minutes=Decimal(60))
    for bot in (v1, v2, V3_BOT):
        for replacement in (None, dict(DONOR_CONFIG, id=999, stop_loss_ticks=77)):
            redis = FakeRedis()

            async def revoked(stop, params):
                if replacement:
                    redis.store[KEY] = json.dumps(donor_payload(replacement))
                else:
                    redis.store.pop(KEY)
                return m.TradeType.BUY.value, PRICE

            await attempt(redis, bot, revoked)
            assert not redis.pushed, 'вход по отозванному/заменённому выбору'

            # Следующая попытка действительно загружает новый конфиг.
            if replacement:
                async def fresh(stop, params):
                    assert params['bot_config'].stop_loss_ticks == 77
                    stop.set()
                    return m.TradeType.BUY.value, PRICE

                await attempt(redis, bot, fresh)
                assert redis.pushed[0]['referral_bot_id'] == 999

    redis = FakeRedis()

    async def timeout(stop, params):
        redis.store.pop(KEY)
        raise asyncio.TimeoutError

    await attempt(redis, v1, timeout)
    assert not redis.pushed
    print('  v1/v2/v3: отзыв и замена перед сигналом запрещают старый вход, новый конфиг подхватывается')


async def check_chain_at_entry():
    for version in (1, 2):
        class ChangedCrud(FakeCrud):
            changed = False

            async def get_sorted_by_profit(self, **kwargs):
                if self.changed and kwargs.get('just_copy_bots' if version == 1 else 'just_copy_bots_v2'):
                    return []
                return await super().get_sorted_by_profit(**kwargs)

        async def signal(stop, params):
            ChangedCrud.changed = True
            return m.TradeType.BUY.value, PRICE

        redis = FakeRedis()
        await attempt(redis, V3_BOT, signal, crud=ChangedCrud)
        assert not redis.pushed, 'v3 вошёл после потери звена цепочки'
    print('  оба звена цепочки v3 перепроверяются непосредственно перед входом')


async def check_open_position_keeps_config():
    redis = FakeRedis()
    provider = AsyncMock()
    provider._read_price.return_value = PRICE
    count = 0

    async def price(**kwargs):
        nonlocal count
        count += 1
        if count == 2:  # Цена закрытия: вход и финальная проверка уже прошли.
            redis.store.pop(KEY)
        return PRICE

    provider.get_price.side_effect = price

    async def signal(stop, params):
        stop.set()
        return m.TradeType.BUY.value, PRICE

    await attempt(redis, V3_BOT, signal, provider=provider)
    assert count == 2 and redis.pushed[0]['referral_bot_id'] == DONOR_ID
    assert Decimal(redis.pushed[0]['balance']) == 990
    print('  после открытия отзыв донора не теряет сделку и не меняет её объём')


async def check_price_during_final_selection():
    # Реальная проверка цепочки требует await SQL. Пока она идёт, сигнал
    # может устареть: писать open_time после запроса со старой ценой нельзя.
    for cached in (False, True):
        for expired in (False, True):
            redis = FakeRedis()
            cache = m.PriceCache(redis) if cached else None
            if cache:
                cache.track(SYMBOL)
                await cache._refresh_once()
            provider = m.PriceProvider(redis, cache=cache)

            class SlowSelection(FakeCrud):
                signal_seen = False

                async def get_sorted_by_profit(self, **kwargs):
                    if self.signal_seen:
                        if expired:
                            redis.store.pop(f'price_snapshot:{SYMBOL}', None)
                        else:
                            redis.store[f'price_snapshot:{SYMBOL}'] = price_snapshot('99')
                        if cache:
                            await cache._refresh_once()
                    return await super().get_sorted_by_profit(**kwargs)

            async def signal(stop, params):
                SlowSelection.signal_seen = True
                stop.set()
                return m.TradeType.BUY.value, PRICE

            # До исправления истёкшая цена приводила бы к вечному ожиданию
            # закрытия. Таймаут ограничивает именно неисправное выполнение.
            await asyncio.wait_for(attempt(
                redis, V3_BOT, signal, provider=provider, crud=SlowSelection,
            ), timeout=3)
            assert not redis.pushed, 'после перепроверки цепочки принят устаревший сигнал'
    print('  смена/истечение цены во время проверки цепочки: запрет входа с кэшем и без')


async def check_missing_donor():
    """Копибот без донора уходит на новый круг, а не падает.

    Пустой пул — обычное состояние: ключ `copy_bot_*` живёт 90 секунд, а
    воркер ходит раз в 30. Симулятор обязан пережить это молча; падение
    здесь роняет цикл бота целиком, и заметно оно только по логам.
    """
    redis = FakeRedis()
    redis.store.pop(KEY, None)

    v1 = V3_BOT._replace(
        id=COPYBOT_V1_ID, copybot_v3_time_in_minutes=None,
        copybot_v3_compound_balance=False,
        copy_bot_min_time_profitability_min=Decimal(30),
    )

    async def signal(stop, params):
        raise AssertionError('без донора вход не должен даже запрашиваться')

    with patch.object(m.asyncio, 'sleep', AsyncMock()):
        await attempt(redis, v1, signal)

    assert not redis.pushed, 'без донора сделка не пишется'

    print('  копибот без донора не роняет цикл')


async def main():
    await check_publication()
    await check_missing_donor()
    await check_wait_cancellation()
    await check_simulator_revocation()
    await check_chain_at_entry()
    await check_open_position_keeps_config()
    await check_price_during_final_selection()


if __name__ == '__main__':
    asyncio.run(main())
