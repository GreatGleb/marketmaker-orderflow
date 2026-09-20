"""Минутные свечи в Redis: `candles:{SYMBOL}`.

Два режима, переключаются `CANDLES_SOURCE`:

* `ws` — поток `@kline_1m` фьючерсов Binance. Исходное поведение;
* `rest` — поллинг фьючерсного `/fapi/v1/klines`. Рабочий режим, пока
  фьючерсный WS с этого IP принимает подписку и не шлёт ни одного
  фрейма: REST `fapi` тем же ограничением не задет;
* `spot_rest` — то же со спота. Нужен, только если цены тоже берутся со
  спота (`MARKET_DATA_SOURCE=spot_ws`): свечи и цены должны быть с
  одного рынка, иначе бот считает волатильность по одному, а торгует по
  другому.

В обоих режимах пишется полная свеча (OHLCV) — формат и разбор в
`app/sub_services/watchers/candle_store.py`. MA-ботам по-прежнему нужны
только цены закрытия, они их оттуда и берут.
"""

import asyncio
import json
import logging

import httpx
import websockets
from datetime import datetime

from sqlalchemy import select, distinct, func

from app.bots.binance_bot import BinanceBot
from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import AssetExchangeSpec, TestBot, WatchedPair
from app.dependencies import redis_context
from app.sub_services.watchers.candle_history import klines_url
from app.sub_services.watchers.candle_store import (
    MIN_HISTORY,
    candle_from_kline,
    candle_from_ws_kline,
    candles_key,
    dump_candles,
    load_candles,
    merge_candle,
)


INTERVAL = "1m"
# Пауза после отказа биржи по лимиту запросов: следующий такой ответ
# стоит уже бана по IP, поэтому ждём заметно дольше обычного интервала.
RATE_LIMIT_SLEEP_SECONDS = 60

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


async def load_tracked_symbols(session) -> tuple[list[str], int]:
    """Пары, по которым нужны свечи, и нужная глубина истории.

    Это пары активных ботов плюс весь `watched_pair`: отбор пары под
    новую стратегию идёт по её же свечам, а бота на ней ещё нет — по
    одним парам существующего парка выбирать было бы не из чего.
    """
    stmt = select(distinct(TestBot.symbol)).where(TestBot.is_active.is_(True))
    result = await session.execute(stmt)
    symbols = {symbol for symbol in result.scalars().all() if symbol}

    stmt = select(AssetExchangeSpec.symbol).join(WatchedPair.asset_exchange)
    result = await session.execute(stmt)
    symbols.update(symbol for symbol in result.scalars().all() if symbol)

    all_symbols = sorted(symbols)

    stmt = select(
        func.max(TestBot.ma_number_of_candles_for_open_order).label("ma_number_open_order"),
        func.max(TestBot.ma_number_of_candles_for_close_order).label("ma_number_close_order"),
    )
    result = await session.execute(stmt)
    ma_numbers = [number for number in (result.first() or ()) if number is not None]
    ma_number = int(max(ma_numbers)) if ma_numbers else 0

    # MIN_HISTORY — снизу: MA-ботов может не быть вовсе, а волатильность
    # считать всё равно нужно.
    count_of_saved_candles = max(ma_number + 10, MIN_HISTORY)

    return all_symbols, count_of_saved_candles


async def build_ws_url(session):
    all_symbols, count_of_saved_candles = await load_tracked_symbols(session)
    logging.info(f'count_of_saved_candles: {count_of_saved_candles}')

    streams = [f"{symbol.lower()}@kline_{INTERVAL}" for symbol in all_symbols]
    stream_path = "/".join(streams)
    return f"wss://fstream.binance.com/stream?streams={stream_path}", all_symbols, count_of_saved_candles


async def save_candle_to_redis(redis, binance_bot, symbol: str, candle: dict, count_of_saved_candles: int):
    key = candles_key(symbol)
    candles = load_candles(await redis.get(key))

    if not candles and binance_bot is not None:
        klines = await binance_bot.get_klines(symbol=symbol, limit=count_of_saved_candles)

        if klines:
            # Последняя свеча ещё не закрыта — её цены поменяются.
            candles = [candle_from_kline(kline) for kline in klines[:-1]]

    candles = merge_candle(candles, candle, count_of_saved_candles)

    logging.info(f'Saving candle {symbol}')

    await redis.set(key, dump_candles(candles))


async def run_websocket_listener():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        ws_url, tracked_symbols, count_of_saved_candles = await build_ws_url(session)

        while True:
            try:
                logging.info(f"Connecting to WebSocket: {ws_url}")
                async with websockets.connect(ws_url) as websocket:
                    logging.info("✅ Connected to Binance WebSocket for klines.")

                    binance_bot = BinanceBot(is_need_prod_for_data=True)

                    while True:
                        async with redis_context() as redis:
                            message = await websocket.recv()
                            data = json.loads(message)

                            kline_data = data.get("data", {}).get("k", {})
                            symbol = kline_data.get("s")

                            if symbol not in tracked_symbols:
                                continue

                            is_closed = kline_data.get("x", False)

                            if is_closed:
                                await save_candle_to_redis(
                                    redis, binance_bot, symbol,
                                    candle_from_ws_kline(kline_data),
                                    count_of_saved_candles,
                                )

            except websockets.exceptions.ConnectionClosedOK:
                logging.info("⚠️ WebSocket closed gracefully. Reconnecting...")
            except websockets.exceptions.ConnectionClosedError as e:
                logging.info(f"❌ WebSocket connection error: {e}. Reconnecting...")
            except Exception as e:
                logging.info(f"❌ General error: {e}. Reconnecting...")
            finally:
                await asyncio.sleep(3)


async def fetch_rest_candles(client, symbol: str, limit: int, market: str) -> list[dict]:
    """Закрытые свечи пары по REST. Пустой список — пара не отдалась."""
    response = await client.get(klines_url(market), params={
        "symbol": symbol, "interval": INTERVAL, "limit": limit,
    })

    if response.status_code in (418, 429):
        raise httpx.HTTPStatusError(
            f"лимит запросов: {response.status_code}",
            request=response.request, response=response,
        )

    response.raise_for_status()
    klines = response.json()

    if not isinstance(klines, list) or not klines:
        return []

    # Последняя свеча текущая: её high/low ещё вырастут.
    return [candle_from_kline(kline) for kline in klines[:-1]]


async def poll_symbol(client, redis, symbol: str, limit: int, semaphore,
                      market: str = "futures") -> bool:
    """Догружает свечи одной пары. False — эта пара не обновилась."""
    async with semaphore:
        try:
            fresh = await fetch_rest_candles(client, symbol, min(limit + 1, 1000), market)
        except httpx.HTTPStatusError as error:
            status = error.response.status_code if error.response is not None else None

            if status in (418, 429):
                raise

            # 400 на паре, которой нет на этом рынке, — не авария:
            # список пар общий, а торгуются они не везде.
            logging.info(f"{symbol}: свечи не получены ({status}).")
            return False
        except Exception as error:
            logging.info(f"{symbol}: свечи не получены ({error}).")
            return False

    if not fresh:
        return False

    key = candles_key(symbol)
    candles = load_candles(await redis.get(key))

    for candle in fresh:
        candles = merge_candle(candles, candle, limit)

    await redis.set(key, dump_candles(candles))
    return True


async def run_rest_poller(market: str = "futures"):
    """REST-поллинг: работает там, где фьючерсный поток молчит."""
    interval = settings.CANDLES_REST_INTERVAL_SEC
    semaphore = asyncio.Semaphore(max(1, int(settings.CANDLES_REST_CONCURRENCY)))
    refresh_interval = settings.CANDLES_SYMBOLS_REFRESH_SEC

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    symbols: list[str] = []
    limit = MIN_HISTORY
    symbols_loaded_at = 0.0

    async with httpx.AsyncClient(timeout=20) as client:
        while True:
            started_at = asyncio.get_event_loop().time()

            if started_at - symbols_loaded_at >= refresh_interval or not symbols:
                # Список пар меняется при заведении ботов, поэтому он
                # перечитывается на ходу, а не один раз на старте.
                try:
                    async with dsm.get_session() as session:
                        symbols, limit = await load_tracked_symbols(session)
                    symbols_loaded_at = started_at
                    logging.info(
                        f"Источник свечей: REST {market}, пар {len(symbols)}, "
                        f"глубина {limit}, опрос раз в {interval} с."
                    )
                except Exception as error:
                    logging.info(f"❌ Не удалось прочитать список пар: {error}.")

            if symbols:
                try:
                    async with redis_context() as redis:
                        results = await asyncio.gather(*[
                            poll_symbol(client, redis, symbol, limit, semaphore, market)
                            for symbol in symbols
                        ], return_exceptions=True)

                    limited = any(isinstance(item, httpx.HTTPStatusError) for item in results)
                    updated = sum(1 for item in results if item is True)
                    logging.info(f"{datetime.now():%H:%M:%S} — обновлено пар: {updated}/{len(symbols)}")

                    if limited:
                        logging.info(
                            f"⚠️ Биржа ответила лимитом запросов, пауза "
                            f"{RATE_LIMIT_SLEEP_SECONDS} с."
                        )
                        await asyncio.sleep(RATE_LIMIT_SLEEP_SECONDS)
                except Exception as error:
                    logging.info(f"❌ Ошибка опроса свечей: {error}.")

            elapsed = asyncio.get_event_loop().time() - started_at
            await asyncio.sleep(max(0.0, interval - elapsed))


if __name__ == "__main__":
    source = (settings.CANDLES_SOURCE or "ws").strip().lower()

    if source == "rest":
        asyncio.run(run_rest_poller("futures"))
    elif source == "spot_rest":
        asyncio.run(run_rest_poller("spot"))
    else:
        if source != "ws":
            logging.info(f"Неизвестный CANDLES_SOURCE={source!r}, использую 'ws'.")
        asyncio.run(run_websocket_listener())
