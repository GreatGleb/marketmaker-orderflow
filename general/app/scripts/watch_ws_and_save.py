import asyncio
import json
import time
import logging

import httpx
import websockets
from decimal import Decimal, InvalidOperation
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

# Binance 2026-04-23 разделил базовые URL фьючерсных вебсокетов по типам
# потока: /public (bookTicker, depth), /market (!ticker@arr, aggTrade,
# kline, markPrice), /private (listenKey). Старый /ws соединение
# принимает и SUBSCRIBE подтверждает, но данные по нему не идут —
# выглядит как молчание биржи, а не как ошибка. Подробности:
# .ai/docs/test-bots/08-gotchas.md.
WS_URL = "wss://fstream.binance.com/market/ws/!ticker@arr"
# Фьючерсные потоки разнесены по двум базовым URL, и подписаться на оба с
# одного сокета нельзя: замер 2026-09-23 показал, что при смешанной
# подписке на /public приходят только bookTicker, а @ticker молча
# проглатывается — ровно тот отказ, что описан в пункте 29 08-gotchas.md.
# Поэтому два соединения: цены с одного, 24-часовая статистика с другого.
FUTURES_BOOK_WS_URL = "wss://fstream.binance.com/public/stream"
FUTURES_TICKER_WS_URL = "wss://fstream.binance.com/market/stream"
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

# Ключи книги в записи истории. Присутствуют всегда, даже пустые:
# `AssetHistoryCrud.bulk_create` вставляет пачку одним многострочным
# INSERT, а набор колонок берётся из первой строки. Разнородная пачка
# ломается двумя разными способами, и оба плохи:
#
# * первая строка БЕЗ этих ключей — INSERT компилируется, и края спреда
#   у всех остальных строк пачки **молча** уходят в NULL;
# * первая строка С ключами, а дальше без — `CompileError`, который
#   `save_filtered_assets` ловит своим `except Exception` и откатывает
#   транзакцию: теряется вся пачка котировок целиком.
#
# Проверено на SQLAlchemy 2.0.41, см. `check_batch_is_homogeneous` в
# tests/test_book_top.py.
BOOK_FIELDS = (
    "best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty",
)


def _positive(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (TypeError, InvalidOperation):
        return None

    return number if number.is_finite() and number > 0 else None


def book_top(item: dict) -> dict:
    """Лучшие цены и объёмы книги из тикера, если источник их отдаёт.

    Пустой результат — норма: фьючерсный `!ticker@arr` и REST
    `/fapi/v1/ticker/24hr` полей bid/ask не содержат. Перевёрнутую или
    нулевую книгу отбрасываем целиком: половина такой пары хуже, чем
    ничего, — по ней посчитают отрицательный спред.
    """
    empty = dict.fromkeys(BOOK_FIELDS)

    bid = _positive(item.get("b"))
    ask = _positive(item.get("a"))

    if bid is None or ask is None or ask <= bid:
        return empty

    return {
        "best_bid_price": bid,
        "best_ask_price": ask,
        "best_bid_qty": _positive(item.get("B")),
        "best_ask_qty": _positive(item.get("A")),
    }


async def _wait_when_db_table_will_free(redis):
    while True:
        is_stopped = await redis.get(f"asset_history:stop")

        if not is_stopped:
            break
        await asyncio.sleep(1)

    return True


async def save_filtered_assets(session: AsyncSession, redis, data: list[dict], is_need_to_use_just_waiting_list_of_assets, source: str = "BINANCE", publish: bool = True):
    """Пишет котировки в историю, попутно публикуя их в Redis.

    `publish=False` — для питателей, которые публикуют сами и на своей
    частоте: фьючерсный слушатель обновляет Redis каждые 50 мс, а историю
    пишет раз в секунду, и повторная публикация тех же событий вернула бы
    «не принято» (срок и монотонность проверяются по времени события) —
    записи молча отфильтровались бы, и история осталась бы пустой.
    Ответственность за то, что в историю идут только принятые события
    (пункт 25 в 08-gotchas.md), переходит на вызывающего.
    """
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

        book = book_top(item)
        record_data.update(book)
        # Симулятор берёт цену из Redis, а не из БД, поэтому проверке
        # post-only и теневому замеру исполнения нужен снимок, а не
        # история. Читатели снимка разбирают его через parse_snapshot и
        # лишние ключи игнорируют.
        if book["best_bid_price"] is not None:
            snapshot["bid"] = str(book["best_bid_price"])
            snapshot["ask"] = str(book["best_ask_price"])

        # if symbol == 'BTCUSDT':
        #     records.append(record_data)
        #     await redis.set(f"price:{symbol}", last_price)

        records.append(record_data)
        snapshots.append(snapshot)

    if publish:
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
                        # Края спреда свежее тех, что пришли с @ticker
                        # раз в секунду, и перетирают их осознанно.
                        item["b"], item["a"] = bid, ask
                        item["B"], item["A"] = data.get("B"), data.get("A")
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


def merge_book_and_ticker(symbol: str, book: dict | None, ticker: dict | None) -> dict | None:
    """Одна котировка из двух потоков: края книги плюс 24-часовая статистика.

    `None` — если книги ещё нет: писать строку без цены незачем.
    Статистики может не быть, и это не повод пропускать котировку — она
    приезжает раз в секунду отдельным соединением и в первые мгновения
    после подписки отстаёт. Пара в этот момент пишется с пустыми
    колонками оборота, но с ценой и книгой.
    """
    if not book:
        return None

    bid, ask = book.get("b"), book.get("a")

    if bid is None or ask is None:
        return None

    item = dict(ticker or {})
    item["s"] = symbol
    # Цена остаётся серединой спреда — той же величиной, по которой
    # посчитаны все накопленные сделки. Края кладём рядом, а не вместо.
    item["c"] = str((Decimal(bid) + Decimal(ask)) / 2)
    item["E"] = book.get("E")
    item["b"], item["B"] = bid, book.get("B")
    item["a"], item["A"] = ask, book.get("A")

    return item


async def _watched_symbols(session, use_watched_filter: bool) -> list[str]:
    """Пары для подписки. Ждёт, а не выходит: на чистой базе список пуст.

    Выйти здесь нельзя по той же причине, что и в спотовом слушателе:
    supervisord после трёх перезапусков пометит процесс FATAL и цены не
    пойдут даже после того, как список наполнят.
    """
    watched_crud = WatchedPairCrud(session)
    asset_crud = AssetExchangeSpecCrud(session)

    while True:
        if use_watched_filter:
            symbols = sorted((await watched_crud.get_symbol_to_id_map()).keys())
        else:
            # Писать можно только туда, где есть asset_exchange_id.
            symbols = sorted((await asset_crud.get_all_symbols_with_id_map()).keys())

        if symbols:
            return symbols

        logging.info(
            f"Подписываться не на что: watched_pair пуст. Жду "
            f"{EMPTY_SYMBOLS_RETRY_SECONDS} с. Наполнить список: "
            f"python -m app.scripts.seed_watched_pairs"
        )
        await asyncio.sleep(EMPTY_SYMBOLS_RETRY_SECONDS)


async def _subscribe(ws, streams: list[str]) -> None:
    """Подписка чанками: длинный URL Binance не примет, а на входящие
    команды у него лимит 5 в секунду."""
    for index in range(0, len(streams), SUBSCRIBE_CHUNK):
        await ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": streams[index:index + SUBSCRIBE_CHUNK],
            "id": index // SUBSCRIBE_CHUNK + 1,
        }))
        await asyncio.sleep(0.3)


async def run_futures_ws_listener():
    """Фьючерсный `@bookTicker`: края книги и тиковая частота.

    Зачем отдельный режим, когда есть `!ticker@arr`. Тот отдаёт снимок
    24-часового тикера примерно раз в секунду и **не содержит bid/ask
    вовсе** — значит ни спреда, ни проверки post-only по нему не
    построить, а прострел короче секунды в него не попадает в принципе.
    `@bookTicker` даёт и края книги, и каждое их изменение.

    Redis и база живут на разных частотах, и это главное решение здесь.
    Замер 2026-09-23: 834 тика в секунду на 55 парах. Писать каждый в
    `asset_history` — 72 млн строк в сутки, чего не выдержит ни диск, ни
    ретеншн. При этом симулятор читает цену **из Redis**, а не из базы, и
    его собственный кэш обновляется раз в 50 мс (`PriceCache`), стратегии
    опрашивают раз в 100 мс. Отсюда:

    * **в Redis — самое свежее по каждой паре раз в 50 мс.** Чаще
      бессмысленно: в ключе всё равно живёт одно значение, монотонность
      отбросит промежуточные;
    * **в историю — одна строка на пару в секунду.** Это та же плотность,
      что давал `!ticker@arr`, то есть ни диск, ни статистика
      волатильности не замечают подмены, но в строке появляются bid и ask.
    """
    publish_interval = settings.MARKET_DATA_PUBLISH_SEC
    history_interval = settings.MARKET_DATA_HISTORY_SEC
    # Только watched_pair, режима «все пары» здесь нет намеренно: на 900
    # символах @bookTicker — это десятки тысяч фреймов в секунду, и ни
    # одна из них, кроме отобранных, всё равно не торгуется.
    use_watched_filter = True

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        symbols = await _watched_symbols(session, use_watched_filter)

        logging.info(
            f"Источник данных: фьючерсный WS Binance, {len(symbols)} пар, "
            f"частота от @bookTicker, last_price = середина спреда, "
            f"публикация в Redis раз в {publish_interval} с, история раз в "
            f"{history_interval} с."
        )

        # Последнее состояние по каждой паре. Не очередь, а именно
        # «последнее»: промежуточные тики никому не нужны — в Redis живёт
        # одно значение, а в историю идёт одна строка за интервал.
        latest_book: dict[str, dict] = {}
        latest_ticker: dict[str, dict] = {}
        # Опубликованное и принятое — то, что имеет право попасть в
        # историю (пункт 25 в 08-gotchas.md).
        pending_history: dict[str, dict] = {}
        # Время последнего отправленного события по паре. Без этого ночью,
        # когда книга стоит, каждые 50 мс улетали бы 55 EVAL-ов, и все до
        # одного Redis отбросил бы по монотонности. Авторитетная проверка
        # всё равно там — здесь только экономия трафика.
        published_at: dict[str, int] = {}

        def merged(symbol: str) -> dict | None:
            return merge_book_and_ticker(
                symbol, latest_book.get(symbol), latest_ticker.get(symbol)
            )

        async def book_reader(ws):
            while True:
                payload = json.loads(await ws.recv())
                data = payload.get("data") or {}
                symbol = data.get("s")

                if symbol:
                    latest_book[symbol] = data

        async def ticker_reader(ws):
            while True:
                payload = json.loads(await ws.recv())
                data = payload.get("data") or {}
                symbol = data.get("s")

                if symbol:
                    latest_ticker[symbol] = data

        async def connection(url: str, streams: list[str], reader, label: str):
            """Своё переподключение на каждый сокет: падение одного из двух
            не должно ронять второй."""
            while True:
                try:
                    async with websockets.connect(
                        url, open_timeout=25, max_queue=None
                    ) as ws:
                        await _subscribe(ws, streams)
                        logging.info(
                            f"✅ {label}: подписано стримов {len(streams)}."
                        )
                        await reader(ws)
                except websockets.exceptions.ConnectionClosedOK:
                    logging.info(f"⚠️ {label}: соединение закрыто. Переподключаюсь...")
                except websockets.exceptions.ConnectionClosedError as e:
                    logging.info(f"❌ {label}: соединение оборвано: {e}. Переподключаюсь...")
                except Exception as e:
                    logging.info(f"❌ {label}: ошибка: {e}. Переподключаюсь...")
                finally:
                    await asyncio.sleep(3)

        async def publisher():
            while True:
                await asyncio.sleep(publish_interval)

                items = []

                for symbol in symbols:
                    item = merged(symbol)

                    if not item or not isinstance(item.get("E"), int):
                        continue

                    if item["E"] <= published_at.get(symbol, 0):
                        continue

                    items.append(item)

                if not items:
                    continue

                snapshots = []
                for item in items:
                    book = book_top(item)
                    snapshot = {
                        "symbol": item["s"], "price": item["c"],
                        "event_time_ms": item["E"], "source": "BINANCE",
                    }
                    if book["best_bid_price"] is not None:
                        snapshot["bid"] = str(book["best_bid_price"])
                        snapshot["ask"] = str(book["best_ask_price"])
                    snapshots.append(snapshot)

                async with redis_context() as redis:
                    accepted = await publish_prices(redis, snapshots)

                # В историю пойдёт только то, что Redis принял: событие
                # свежее и новее предыдущего. Остальное — повтор того же
                # состояния книги, писать его незачем.
                for item, ok in zip(items, accepted):
                    published_at[item["s"]] = item["E"]

                    if ok:
                        pending_history[item["s"]] = item

        async def historian():
            while True:
                await asyncio.sleep(history_interval)

                if not pending_history:
                    continue

                batch = list(pending_history.values())
                pending_history.clear()

                async with redis_context() as redis:
                    await save_filtered_assets(
                        session, redis, batch, use_watched_filter,
                        source="BINANCE", publish=False,
                    )

        book_streams = [f"{symbol.lower()}@bookTicker" for symbol in symbols]
        ticker_streams = [f"{symbol.lower()}@ticker" for symbol in symbols]

        await asyncio.gather(
            connection(FUTURES_BOOK_WS_URL, book_streams, book_reader, "bookTicker"),
            connection(FUTURES_TICKER_WS_URL, ticker_streams, ticker_reader, "ticker"),
            publisher(),
            historian(),
        )


if __name__ == "__main__":
    source = (settings.MARKET_DATA_SOURCE or "ws").strip().lower()

    if source == "rest":
        asyncio.run(run_rest_poller())
    elif source == "futures_ws":
        asyncio.run(run_futures_ws_listener())
    elif source == "spot_ws":
        asyncio.run(run_spot_ws_listener())
    else:
        if source != "ws":
            logging.info(f"Неизвестный MARKET_DATA_SOURCE={source!r}, использую 'ws'.")
        asyncio.run(run_websocket_listener())
