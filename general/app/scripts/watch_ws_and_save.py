import asyncio
import json
import time
import logging

import httpx
import websockets
from decimal import Decimal
from datetime import datetime
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.crud.asset_history import AssetHistoryCrud
from app.crud.watched_pair import WatchedPairCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.dependencies import redis_context
from app.scripts.seed_binance_data import seed_binance_data
from app.sub_services.watchers.price_snapshot import parse_snapshot, publish_prices

WS_URL = "wss://fstream.binance.com/ws/!ticker@arr"
REST_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
SPOT_WS_URL = "wss://stream.binance.com:9443/stream"
SPOT_EXCHANGE_INFO_URL = "https://api.binance.com/api/v3/exchangeInfo"
SUBSCRIBE_CHUNK = 150
# Если БД не успевает, буфер не должен расти в память бесконечно.
MAX_SPOT_BUFFER = 20000
# Как часто перепроверять список пар, если подписываться пока не на что.
EMPTY_SYMBOLS_RETRY_SECONDS = 30

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def _wait_when_db_table_will_free(redis):
    while True:
        is_stopped = await redis.get(f"asset_history:stop")

        if not is_stopped:
            break
        await asyncio.sleep(1)

    return True


async def save_filtered_assets(session: AsyncSession, redis, data: list[dict], is_need_to_use_just_waiting_list_of_assets, source: str = "BINANCE"):
    try:
        history_crud = AssetHistoryCrud(session)
        watched_crud = WatchedPairCrud(session)
        asset_crud = AssetExchangeSpecCrud(session)

        if is_need_to_use_just_waiting_list_of_assets:
            symbol_to_id = await watched_crud.get_symbol_to_id_map()
            symbols_set = set(symbol_to_id.keys())
            logging.info(f"symbols_set: {symbols_set}")
        else:
            symbol_to_id = await asset_crud.get_all_symbols_with_id_map()
            symbols_set = set(symbol_to_id.keys())
            # logging.info(f"symbol_to_id: {symbol_to_id} symbols_set: {symbols_set}")

    except Exception as e:
        await session.rollback()
        logging.info(f"❌ Error DB: {e}")
        return

    records = []
    snapshots = []
    rejected = 0

    for item in data:
        symbol = item.get("s")
        if not isinstance(symbol, str):
            continue

        if is_need_to_use_just_waiting_list_of_assets:
            if symbol not in symbols_set:
                continue
        else:
            if not symbol.endswith("USDT"):
                continue

        snapshot = {
            "symbol": symbol, "price": item.get("c"),
            "event_time_ms": item.get("E"), "source": source,
        }
        if parse_snapshot(snapshot) is None:
            rejected += 1
            continue

        try:
            asset_exchange_id = symbol_to_id[symbol]
        except:
            logging.info(f"error with {symbol}")
            await seed_binance_data()

            if is_need_to_use_just_waiting_list_of_assets:
                symbol_to_id = await watched_crud.get_symbol_to_id_map()
                symbols_set = set(symbol_to_id.keys())
            else:
                symbol_to_id = await asset_crud.get_all_symbols_with_id_map()
                symbols_set = set(symbol_to_id.keys())

            asset_exchange_id = symbol_to_id[symbol]
        last_price = item.get("c")

        record_data = {
            "symbol": symbol,
            "source": source,
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
        snapshots.append(snapshot)

    accepted = await publish_prices(redis, snapshots)
    rejected += sum(not ok for ok in accepted)
    records = [record for record, ok in zip(records, accepted) if ok]
    if rejected:
        logging.info(f"Котировки: отклонено {rejected} устаревших, повторных или неверных событий.")
    if not records:
        # Чтение списка пар тоже открывает транзакцию. При остановке
        # обновлений биржи не держим её до следующей принятой котировки.
        await session.rollback()
        return

    is_stopped = await redis.get(f"asset_history:stop")
    if is_stopped:
        logging.info(f'It is stopped')
        await _wait_when_db_table_will_free(redis)

    try:
        await history_crud.bulk_create(records)
        await session.commit()
        logging.info(f"✅ Saved {len(records)} asset history records.")
    except Exception as e:
        await session.rollback()
        logging.info(f"❌ Error DB: {e}")
        return


async def run_websocket_listener():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        while True:
            try:
                logging.info("Attempting to connect to WebSocket...")
                async with websockets.connect(WS_URL) as ws:
                    websocket = ws
                    logging.info("✅ WebSocket connected.")

                    target_datetime = datetime(2025, 10, 20, 20, 0, 0)

                    last_check_time = time.time()
                    interval = 60
                    is_need_to_use_just_waiting_list_of_assets = False

                    while True:
                        current_time = time.time()
                        if current_time - last_check_time >= interval:
                            logging.info(f"\nПрошло {interval} секунд. Выполняем проверку...")

                            current_actual_datetime = datetime.now()
                            if current_actual_datetime >= target_datetime:
                                is_need_to_use_just_waiting_list_of_assets = True
                                logging.info(
                                    f"Текущее время: {current_actual_datetime}. Уже {target_datetime.strftime('%d.%m.%Y %H:%M')} или позже.")
                            else:
                                logging.info(
                                    f"Текущее время: {current_actual_datetime}. Ещё не наступило {target_datetime.strftime('%d.%m.%Y %H:%M')}.")

                            last_check_time = current_time

                        async with redis_context() as redis:
                            message = await websocket.recv()
                            data = json.loads(message)
                            if isinstance(data, list):
                                await save_filtered_assets(
                                    session,
                                    redis,
                                    data,
                                    is_need_to_use_just_waiting_list_of_assets
                                )
            except websockets.exceptions.ConnectionClosedOK:
                logging.info("⚠️ WebSocket connection closed gracefully. Reconnecting...")
            except websockets.exceptions.ConnectionClosedError as e:
                logging.info(f"❌ WebSocket connection closed with error: {e}. Reconnecting...")
            except Exception as e:
                logging.info(f"❌ General error: {e}. Reconnecting...")
            finally:
                await asyncio.sleep(3)


_REST_TO_WS_FIELDS = {
    "symbol": "s",
    "lastPrice": "c",
    "priceChange": "p",
    "priceChangePercent": "P",
    "volume": "v",
    "quoteVolume": "q",
    "weightedAvgPrice": "w",
    "highPrice": "h",
    "lowPrice": "l",
    "openTime": "O",
    "closeTime": "C",
}


def _rest_tickers_to_ws_format(tickers: list[dict]) -> list[dict]:
    """Приводит ответ REST /fapi/v1/ticker/24hr к формату потока !ticker@arr,
    чтобы save_filtered_assets работала без изменений."""
    data = []
    for ticker in tickers:
        item = {
            ws_key: ticker.get(rest_key)
            for rest_key, ws_key in _REST_TO_WS_FIELDS.items()
        }
        item["E"] = ticker.get("closeTime")
        data.append(item)

    return data


async def run_rest_poller():
    interval = settings.MARKET_DATA_REST_INTERVAL_SEC
    logging.info(f"Источник данных: REST {REST_URL}, опрос раз в {interval} с.")

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        async with httpx.AsyncClient(timeout=20) as client:
            target_datetime = datetime(2025, 10, 20, 20, 0, 0)

            last_check_time = time.time()
            check_interval = 60
            is_need_to_use_just_waiting_list_of_assets = False

            while True:
                started_at = time.time()

                if started_at - last_check_time >= check_interval:
                    logging.info(f"\nПрошло {check_interval} секунд. Выполняем проверку...")

                    current_actual_datetime = datetime.now()
                    if current_actual_datetime >= target_datetime:
                        is_need_to_use_just_waiting_list_of_assets = True
                        logging.info(
                            f"Текущее время: {current_actual_datetime}. Уже {target_datetime.strftime('%d.%m.%Y %H:%M')} или позже.")
                    else:
                        logging.info(
                            f"Текущее время: {current_actual_datetime}. Ещё не наступило {target_datetime.strftime('%d.%m.%Y %H:%M')}.")

                    last_check_time = started_at

                try:
                    response = await client.get(REST_URL)
                    response.raise_for_status()
                    tickers = response.json()

                    if isinstance(tickers, list):
                        # Возраст и монотонность проверяются в общем пути
                        # публикации, с сохранением метки времени в Redis.
                        async with redis_context() as redis:
                            await save_filtered_assets(
                                session, redis, _rest_tickers_to_ws_format(tickers),
                                is_need_to_use_just_waiting_list_of_assets,
                            )
                except Exception as e:
                    logging.info(f"❌ REST polling error: {e}. Повтор через {interval} с.")

                elapsed = time.time() - started_at
                await asyncio.sleep(max(0.0, interval - elapsed))


async def run_spot_ws_listener():
    """Спотовый поток Binance: максимальная частота там, где боевой USDT-M push недоступен.
    Цена берётся из @aggTrade (каждая сделка), 24h-статистика — из @ticker (раз в секунду)."""
    flush_interval = settings.MARKET_DATA_SPOT_FLUSH_SEC
    spot_source = settings.MARKET_DATA_SPOT_SOURCE

    symbols_mode = settings.MARKET_DATA_SPOT_SYMBOLS.strip().lower()
    if symbols_mode not in ("watched", "all"):
        logging.info(
            f"Неизвестный MARKET_DATA_SPOT_SYMBOLS={symbols_mode!r}, использую 'watched'."
        )
        symbols_mode = "watched"

    use_watched_filter = symbols_mode == "watched"

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        # Спотовые пары в статусе TRADING.
        async with httpx.AsyncClient(timeout=30) as client:
            info = (await client.get(SPOT_EXCHANGE_INFO_URL)).json()
        spot_symbols = {
            item["symbol"] for item in info.get("symbols", [])
            if item.get("status") == "TRADING"
        }

        watched_crud = WatchedPairCrud(session)
        asset_crud = AssetExchangeSpecCrud(session)

        # Ждём, а не выходим: на чистой базе watched_pair пуст, его наполняет
        # seed_watched_pairs. Если здесь сделать return, supervisord после
        # трёх перезапусков пометит процесс FATAL и перестанет его поднимать —
        # и цены не пойдут даже после наполнения списка.
        while True:
            if use_watched_filter:
                wanted = sorted((await watched_crud.get_symbol_to_id_map()).keys())
            else:
                # Писать можно только туда, где есть asset_exchange_id,
                # иначе save_filtered_assets уйдёт в пересид по KeyError.
                wanted = sorted((await asset_crud.get_all_symbols_with_id_map()).keys())

            symbols = [symbol for symbol in wanted if symbol in spot_symbols]
            skipped = len(wanted) - len(symbols)

            if symbols:
                break

            reason = (
                "watched_pair пуст" if not wanted
                else "ни одной пары из watched_pair нет на споте"
            )
            logging.info(
                f"Подписываться не на что: {reason}. Жду "
                f"{EMPTY_SYMBOLS_RETRY_SECONDS} с. Наполнить список: "
                f"python -m app.scripts.seed_watched_pairs"
            )
            await asyncio.sleep(EMPTY_SYMBOLS_RETRY_SECONDS)

        logging.info(
            f"Режим пар: {symbols_mode}. Подписываюсь на {len(symbols)} пар, "
            f"пропущено (нет на споте): {skipped}."
        )

        price_stream = settings.MARKET_DATA_SPOT_STREAM.strip()
        if price_stream not in ("bookTicker", "aggTrade"):
            logging.info(
                f"Неизвестный MARKET_DATA_SPOT_STREAM={price_stream!r}, использую 'bookTicker'."
            )
            price_stream = "bookTicker"

        stream_names = []
        for symbol in symbols:
            stream_names.append(f"{symbol.lower()}@{price_stream}")
            stream_names.append(f"{symbol.lower()}@ticker")

        price_meaning = (
            "середина спреда (bid+ask)/2" if price_stream == "bookTicker"
            else "цена сделки"
        )
        logging.info(
            f"Источник данных: спотовый WS Binance, {len(symbols)} пар, "
            f"{len(stream_names)} стримов, частота от @{price_stream}, "
            f"last_price = {price_meaning}, сброс в БД раз в {flush_interval} с, "
            f"source={spot_source}."
        )

        while True:
            buffer = []
            latest_ticker = {}

            async def reader(ws):
                while True:
                    payload = json.loads(await ws.recv())
                    data = payload.get("data") or {}
                    symbol = data.get("s")

                    if not symbol:
                        continue

                    # У спотового @bookTicker нет полей "e" и "E",
                    # поэтому тип определяем по имени стрима.
                    stream_name = payload.get("stream", "").split("@")[-1]

                    if stream_name == "ticker":
                        latest_ticker[symbol] = data
                        continue

                    item = dict(latest_ticker.get(symbol, {}))
                    item["s"] = symbol

                    if stream_name == "bookTicker":
                        bid, ask = data.get("b"), data.get("a")
                        if bid is None or ask is None:
                            continue
                        item["c"] = str((Decimal(bid) + Decimal(ask)) / 2)
                        item["E"] = int(time.time() * 1000)
                    else:
                        item["c"] = data.get("p")
                        item["E"] = data.get("E")

                    if len(buffer) >= MAX_SPOT_BUFFER:
                        dropped = len(buffer) - MAX_SPOT_BUFFER // 2
                        del buffer[:dropped]
                        logging.info(
                            f"⚠️ Буфер переполнен, БД не успевает: отброшено {dropped} тиков. "
                            f"Уменьшите число пар или увеличьте MARKET_DATA_SPOT_FLUSH_SEC."
                        )

                    buffer.append(item)

            async def flusher():
                while True:
                    await asyncio.sleep(flush_interval)

                    if not buffer:
                        continue

                    batch = list(buffer)
                    buffer.clear()

                    async with redis_context() as redis:
                        await save_filtered_assets(
                            session, redis, batch, use_watched_filter,
                            source=spot_source
                        )

            try:
                logging.info("Attempting to connect to spot WebSocket...")
                async with websockets.connect(
                    SPOT_WS_URL, open_timeout=25, max_queue=None
                ) as ws:
                    # Подписка чанками: длинный URL Binance не примет,
                    # а на входящие команды у него лимит 5/с.
                    for index in range(0, len(stream_names), SUBSCRIBE_CHUNK):
                        chunk = stream_names[index:index + SUBSCRIBE_CHUNK]
                        await ws.send(json.dumps({
                            "method": "SUBSCRIBE",
                            "params": chunk,
                            "id": index // SUBSCRIBE_CHUNK + 1,
                        }))
                        await asyncio.sleep(0.3)

                    logging.info(
                        f"✅ Spot WebSocket connected, подписано стримов: {len(stream_names)}."
                    )
                    await asyncio.gather(reader(ws), flusher())
            except websockets.exceptions.ConnectionClosedOK:
                logging.info("⚠️ Spot WebSocket closed gracefully. Reconnecting...")
            except websockets.exceptions.ConnectionClosedError as e:
                logging.info(f"❌ Spot WebSocket closed with error: {e}. Reconnecting...")
            except Exception as e:
                logging.info(f"❌ Spot general error: {e}. Reconnecting...")
            finally:
                await asyncio.sleep(3)


if __name__ == "__main__":
    source = (settings.MARKET_DATA_SOURCE or "ws").strip().lower()

    if source == "rest":
        asyncio.run(run_rest_poller())
    elif source == "spot_ws":
        asyncio.run(run_spot_ws_listener())
    else:
        if source != "ws":
            logging.info(f"Неизвестный MARKET_DATA_SOURCE={source!r}, использую 'ws'.")
        asyncio.run(run_websocket_listener())
