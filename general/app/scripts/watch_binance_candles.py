import asyncio
import json
import websockets
from datetime import datetime

from sqlalchemy import select, distinct, func

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import TestBot
from app.dependencies import redis_context


INTERVAL = "1m"


async def build_ws_url(session):
    stmt = select(distinct(TestBot.symbol))
    result = await session.execute(stmt)
    all_symbols = result.scalars().all()

    stmt = select(
        func.max(TestBot.ma_number_of_candles_for_open_order).label("ma_number_open_order"),
        func.max(TestBot.ma_number_of_candles_for_close_order).label("ma_number_close_order"),
    )
    result = await session.execute(stmt)
    ma_numbers = result.first()
    ma_number = max(ma_numbers)

    count_of_saved_candles = ma_number + 10
    streams = [f"{symbol.lower()}@kline_{INTERVAL}" for symbol in all_symbols]
    stream_path = "/".join(streams)
    return f"wss://fstream.binance.com/stream?streams={stream_path}", all_symbols, count_of_saved_candles


async def save_candle_to_redis(redis, symbol: str, candle: dict, count_of_saved_candles: int):
    key = f"candles:{symbol}"
    existing = await redis.get(key)
    try:
        candle_list = json.loads(existing) if existing else []
    except Exception:
        candle_list = []

    candle_list.append(candle)
    if len(candle_list) > count_of_saved_candles:
        candle_list = candle_list[-count_of_saved_candles:]

    print(f'Saving candle {candle_list}')

    await redis.set(key, json.dumps(candle_list))


async def run_websocket_listener():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        ws_url, tracked_symbols, count_of_saved_candles = await build_ws_url(session)

        while True:
            try:
                print(f"Connecting to WebSocket: {ws_url}")
                async with websockets.connect(ws_url) as websocket:
                    print("✅ Connected to Binance WebSocket for klines.")

                    while True:
                        async with redis_context() as redis:
                            message = await websocket.recv()
                            data = json.loads(message)

                            kline_data = data.get("data", {}).get("k", {})
                            symbol = kline_data.get("s")

                            if symbol not in tracked_symbols:
                                continue

                            candle_close_price = kline_data.get("c")

                            is_closed = kline_data.get("x", False)

                            if is_closed:
                                # event_time = datetime.fromtimestamp(kline_data.get("T") / 1000)
                                await save_candle_to_redis(redis, symbol, candle_close_price, count_of_saved_candles)
                            else:
                                await redis.set(f"candle_current:{symbol}", candle_close_price)

            except websockets.exceptions.ConnectionClosedOK:
                print("⚠️ WebSocket closed gracefully. Reconnecting...")
            except websockets.exceptions.ConnectionClosedError as e:
                print(f"❌ WebSocket connection error: {e}. Reconnecting...")
            except Exception as e:
                print(f"❌ General error: {e}. Reconnecting...")
            finally:
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_websocket_listener())