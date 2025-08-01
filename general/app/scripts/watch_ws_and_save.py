import asyncio
import json
import websockets
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.crud.asset_history import AssetHistoryCrud
from app.crud.watched_pair import WatchedPairCrud
from app.dependencies import redis_context

WS_URL = "wss://fstream.binance.com/ws/!ticker@arr"


async def _wait_when_db_table_will_free(redis):
    while True:
        is_stopped = await redis.get(f"asset_history:stop")

        if not is_stopped:
            break
        await asyncio.sleep(1)

    return True


async def save_filtered_assets(session: AsyncSession, redis, data: list[dict]):
    try:
        history_crud = AssetHistoryCrud(session)
        watched_crud = WatchedPairCrud(session)

        symbol_to_id = await watched_crud.get_symbol_to_id_map()
        symbols_set = set(symbol_to_id.keys())
    except Exception as e:
        await session.rollback()
        print(f"❌ Error DB: {e}")
        return

    records = []

    for item in data:
        symbol = item.get("s")

        if symbol not in symbols_set:
            continue

        asset_exchange_id = symbol_to_id[symbol]
        last_price = item.get("c")

        record_data = {
            "symbol": symbol,
            "source": "BINANCE",
            "last_price": last_price,
            "asset_exchange_id": asset_exchange_id,
            "price_change_24h": item.get("p"),
            "price_change_percent_24h": item.get("P"),
            "base_asset_volume_24h": item.get("v"),
            "quote_asset_volume_24h": item.get("q"),
            "weighted_avg_price_24h": item.get("w"),
            "price_high_24h": item.get("h"),
            "price_low_24h": item.get("l"),
            "event_time": datetime.fromtimestamp(item.get("E") / 1000),
            "statistics_open_time": item.get("O"),
            "statistics_close_time": item.get("C"),
        }

        # if symbol == 'BTCUSDT':
        #     records.append(record_data)
        #     await redis.set(f"price:{symbol}", last_price)

        records.append(record_data)
        await redis.set(f"price:{symbol}", last_price)

    is_stopped = await redis.get(f"asset_history:stop")
    if is_stopped:
        print(f'It is stopped')
        await _wait_when_db_table_will_free(redis)

    try:
        await history_crud.bulk_create(records)
        await session.commit()
        print(f"✅ Saved {len(records)} asset history records.")
    except Exception as e:
        await session.rollback()
        print(f"❌ Error DB: {e}")
        return


async def run_websocket_listener():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        while True:
            try:
                print("Attempting to connect to WebSocket...")
                async with websockets.connect(WS_URL) as ws:
                    websocket = ws
                    print("✅ WebSocket connected.")

                    while True:
                        async with redis_context() as redis:
                            message = await websocket.recv()
                            data = json.loads(message)
                            if isinstance(data, list):
                                await save_filtered_assets(
                                    session,
                                    redis,
                                    data,
                                )
            except websockets.exceptions.ConnectionClosedOK:
                print("⚠️ WebSocket connection closed gracefully. Reconnecting...")
            except websockets.exceptions.ConnectionClosedError as e:
                print(f"❌ WebSocket connection closed with error: {e}. Reconnecting...")
            except Exception as e:
                print(f"❌ General error: {e}. Reconnecting...")
            finally:
                await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_websocket_listener())