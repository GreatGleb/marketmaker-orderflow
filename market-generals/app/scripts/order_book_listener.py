import asyncio
import json
import websockets
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.crud.asset_order_book import AssetOrderBookCrud
from app.crud.watched_pair import WatchedPairCrud


WS_URL = "wss://fstream.binance.com/stream?streams="


def build_stream_url(symbols: list[str]) -> str:
    """Формирует URL для стрима нескольких пар"""
    streams = "/".join(f"{s.lower()}@depth" for s in symbols)
    return WS_URL + streams


async def save_order_books(session: AsyncSession, messages: list[dict]):
    crud = AssetOrderBookCrud(session)
    watched_crud = WatchedPairCrud(session)

    symbol_to_id = await watched_crud.get_symbol_to_id_map()
    symbols_set = set(symbol_to_id.keys())

    records = []

    for message in messages:
        stream = message.get("stream")
        data = message.get("data")

        if not stream or not data:
            continue

        symbol = data.get("s")
        if symbol not in symbols_set:
            continue

        record = {
            "asset_exchange_id": symbol_to_id[symbol],
            "transaction_time": data.get("T"),
            "bids": data.get("b", []),
            "asks": data.get("a", []),
        }
        records.append(record)

    if records:
        await crud.bulk_create(records)
        await session.commit()
        print(f"✅ Saved {len(records)} order book records.")


async def run_order_book_listener():
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        watched_crud = WatchedPairCrud(session)
        symbol_to_id = await watched_crud.get_symbol_to_id_map()

    ws_url = build_stream_url(list(symbol_to_id.keys()))

    async with websockets.connect(ws_url) as websocket:
        dsm = DatabaseSessionManager.create(settings.DB_URL)
        async with dsm.get_session() as session:
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)

                    await save_order_books(session, [data])
                except Exception as e:
                    print(f"❌ WebSocket error: {e}")
                    await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(run_order_book_listener())
