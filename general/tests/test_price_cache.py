import asyncio, logging
from decimal import Decimal
from tests.price_fixtures import price_snapshot
from app.sub_services.watchers.price_provider import PriceCache, PriceProvider, PriceWatcher

logging.basicConfig(format='  %(message)s', level=logging.INFO)

class FakeRedis:
    def __init__(self, store): self.store, self.mgets, self.gets = store, 0, 0
    async def mget(self, keys):
        self.mgets += 1
        return [self.store.get(k) for k in keys]
    async def get(self, key):
        self.gets += 1
        return self.store.get(key)

async def main():
    print("=== 1. кэш отдаёт цену, боты не ходят в Redis ===")
    r = FakeRedis({"price_snapshot:AUSDT": price_snapshot("1.5"), "price_snapshot:BUSDT": price_snapshot("2.5")})
    cache = PriceCache(redis=r)
    cache.start()
    provider = PriceProvider(redis=r, cache=cache)

    prices = await asyncio.gather(*[provider.get_price("AUSDT") for _ in range(500)])
    print(f"  500 ботов получили {set(prices)}")
    print(f"  прямых GET в Redis: {r.gets}, MGET: {r.mgets}")
    assert set(prices) == {Decimal("1.5")} and r.gets == 0

    print("\n=== 2. ключ протух -> бот ждёт, а не берёт старую цену ===")
    r.store.pop("price_snapshot:AUSDT")
    await asyncio.sleep(0.15)
    print(f"  в кэше после протухания: {cache.get('AUSDT')}")
    assert cache.get("AUSDT") is None

    waiting = asyncio.create_task(provider.get_price("AUSDT"))
    await asyncio.sleep(0.3)
    print(f"  бот всё ещё ждёт: {not waiting.done()}")
    assert not waiting.done()

    r.store["price_snapshot:AUSDT"] = price_snapshot("9.9")
    print(f"  после возврата цены: {await asyncio.wait_for(waiting, 3)}")

    print("\n=== 3. новая пара подхватывается на ходу ===")
    r.store["price_snapshot:NEWUSDT"] = price_snapshot("7.7")
    price = await asyncio.wait_for(provider.get_price("NEWUSDT"), 3)
    print(f"  NEWUSDT = {price}")
    assert price == Decimal("7.7")

    print("\n=== 4. PriceWatcher переиспользует провайдер с кэшем ===")
    watcher = PriceWatcher(redis=r, price_provider=provider)
    assert watcher.price_provider is provider
    gets_before = r.gets
    await watcher.price_provider.get_price("BUSDT")
    print(f"  прямых GET: {r.gets - gets_before}")
    assert r.gets == gets_before

    print("\n=== 5. без кэша поведение прежнее (боевой binance_bot) ===")
    plain = PriceProvider(redis=r)
    gets_before = r.gets
    price = await plain.get_price("BUSDT")
    print(f"  цена {price}, прямых GET: {r.gets - gets_before}")
    assert price == Decimal("2.5") and r.gets == gets_before + 1

    print("\n=== 6. ошибка Redis не убивает фоновый таск ===")
    class BrokenRedis(FakeRedis):
        async def mget(self, keys):
            self.mgets += 1
            raise RuntimeError("Redis недоступен")
    broken = BrokenRedis({})
    c2 = PriceCache(redis=broken)
    c2.REFRESH_INTERVAL_SECONDS = 0.01
    c2.ERROR_RETRY_SECONDS = 0.01
    c2.track("XUSDT")
    c2.start()
    await asyncio.sleep(0.2)
    print(f"  попыток обновления: {broken.mgets}, таск жив: {not c2._task.done()}")
    assert broken.mgets > 2 and not c2._task.done()

    print("\nOK: кэш работает, TTL уважается, прямое чтение проверено")

asyncio.run(main())
