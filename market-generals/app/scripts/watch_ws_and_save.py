import asyncio
import json
import websockets
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.crud.asset_history import AssetHistoryCrud
from app.crud.watched_pair import WatchedPairCrud


WS_URL = "wss://fstream.binance.com/ws/!ticker@arr"


async def save_filtered_assets(session: AsyncSession, data: list[dict]):
    history_crud = AssetHistoryCrud(session)
    watched_crud = WatchedPairCrud(session)

    symbol_to_id = await watched_crud.get_symbol_to_id_map()
    symbols_set = set(symbol_to_id.keys())

    records = []

    for item in data:
        symbol = item.get("s")

        if symbol not in symbols_set:
            continue

        asset_exchange_id = symbol_to_id[symbol]

        record_data = {
            "symbol": symbol,
            "source": "BINANCE",
            "last_price": item.get("c"),
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

        records.append(record_data)

    await history_crud.bulk_create(records)
    await session.commit()
    print(f"✅ Saved {len(records)} asset history records.")


async def run_websocket_listener():

    async with websockets.connect(WS_URL) as websocket:
        dsm = DatabaseSessionManager.create(settings.DB_URL)
        async with dsm.get_session() as session:
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    if isinstance(data, list):
                        await save_filtered_assets(
                            session,
                            data,
                        )
                except Exception as e:
                    print(f"❌ Error: {e}")
                    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_websocket_listener())
