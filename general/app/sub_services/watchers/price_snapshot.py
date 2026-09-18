"""Цена и время события передаются вместе; повтор не продлевает срок жизни."""

import json
import time
from decimal import Decimal, InvalidOperation

PRICE_MAX_AGE_MS = 120_000
PRICE_FUTURE_TOLERANCE_MS = 5_000


def parse_snapshot(value, now_ms: int | None = None) -> tuple[Decimal, int] | None:
    """Старые числовые ключи без времени события намеренно не принимаются."""
    try:
        data = json.loads(value) if isinstance(value, (str, bytes)) else value
        event_ms = data["event_time_ms"]
        if type(event_ms) is not int:
            return None
        price = Decimal(str(data["price"]))
        if not price.is_finite() or price <= 0:
            return None
        now_ms = int(time.time() * 1000) if now_ms is None else now_ms
        age = now_ms - event_ms
        if not -PRICE_FUTURE_TOLERANCE_MS <= age < PRICE_MAX_AGE_MS:
            return None
        return price, event_ms
    except (ValueError, TypeError, KeyError, InvalidOperation):
        return None


# Проверка и публикация атомарны, в том числе между перезапусками питателя.
# TIME проверяет возраст непосредственно в Redis; PXAT отсчитывает срок от
# события биржи, а не от получения HTTP-ответа. Числовой ключ оставлен для
# диагностики, потребители читают только снимок с меткой времени.
PUBLISH_PRICE_LUA = """
local event_ms = tonumber(ARGV[2])
local clock = redis.call('TIME')
local now_ms = tonumber(clock[1]) * 1000 + math.floor(tonumber(clock[2]) / 1000)
if event_ms <= now_ms - tonumber(ARGV[4]) or event_ms > now_ms + tonumber(ARGV[5]) then
    return 0
end
local previous = redis.call('GET', KEYS[1])
if previous then
    local ok, snapshot = pcall(cjson.decode, previous)
    if ok and type(snapshot) == 'table' and tonumber(snapshot.event_time_ms) then
        if event_ms <= tonumber(snapshot.event_time_ms) then return 0 end
    end
end
local expires_at = event_ms + tonumber(ARGV[4])
redis.call('SET', KEYS[1], ARGV[3], 'PXAT', expires_at)
redis.call('SET', KEYS[2], ARGV[1], 'PXAT', expires_at)
return 1
"""


async def publish_prices(redis, snapshots: list[dict]) -> list[bool]:
    """Для каждого события возвращает, принято ли оно в текущую историю."""
    if not snapshots:
        return []
    async with redis.pipeline(transaction=False) as pipe:
        for snapshot in snapshots:
            symbol = snapshot["symbol"]
            pipe.eval(
                PUBLISH_PRICE_LUA, 2, f"price_snapshot:{symbol}", f"price:{symbol}",
                snapshot["price"], snapshot["event_time_ms"], json.dumps(snapshot),
                PRICE_MAX_AGE_MS, PRICE_FUTURE_TOLERANCE_MS,
            )
        return [bool(result) for result in await pipe.execute()]
