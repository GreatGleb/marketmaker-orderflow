from time import time

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

# from app import logger
from app.dependencies import get_redis
from app.routing.api import router as api_router


async def _calculate_rpm(redis: Redis = Depends(get_redis)) -> int:
    current_minute = int(time() / 60) * 60
    res = await redis.incr(f"rpm:{current_minute}")
    if res == 1:
        await redis.expire(f"rpm:{current_minute}", 600)
    return res


router = APIRouter(dependencies=[Depends(_calculate_rpm)])
router.include_router(api_router)


@router.get("/internal/rpm", include_in_schema=False)
async def get_current_rpm(redis: Redis = Depends(get_redis)):
    stats = []
    for i in range(10):
        minute = int(time() / 60) * 60 - (1 + i) * 60
        key = f"rpm:{minute}"

        try:
            val = await redis.get(key)
        except Exception as e:
            print(str(e))
            # logger.error(f"Redis error [get_current_rpm]: {str(e)}")
            continue

        if val is None:
            continue

        stats.append(int(val))

    return {
        "ok": True,
        "data": {
            "rpm": sum(stats) / len(stats) if len(stats) > 0 else 0,
            "probes": len(stats),
            "min": min(*stats) if len(stats) > 1 else 0,
            "max": max(*stats) if len(stats) > 1 else 0,
        },
    }
