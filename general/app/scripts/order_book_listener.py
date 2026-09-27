"""Слушатель стакана фьючерсов: `@depth20@100ms` → Redis и суточные файлы.

Что изменилось и почему (переписано 2026-09-24).

* **Поток.** Было `@depth` — это дельты книги («что изменилось»), по ним
  OBI не считается: нужен либо готовый снимок, либо снимок по REST плюс
  наложение дельт с проверкой `U`/`u`. Стало `@depth20@100ms` — двадцать
  уровней с каждой стороны, готовые.
* **Куда пишем.** Было — строка в Postgres с коммитом на каждое
  сообщение. На 55 парах это ~550 вставок в секунду с двумя JSON-массивами
  в каждой; база этого не заслужила. Стало: в рантайм — Redis (последний
  снимок на пару, короткий TTL), в архив — суточные файлы на диске.
* **Почему файлы, а не база с поднятым ретеншном.**
  `RETENTION_ORDER_BOOK_HOURS` — сутки, чистку гоняет celery-beat, и она
  сотрёт ровно то, ради чего сбор поднят. Поднять срок можно, но на
  сервере `asset_history` вместе со стаканом занимала 542 МБ из 720 при
  сроке в сутки — месяц такого в Postgres на ноутбуке держать незачем.

Таблица `asset_order_book` после этой правки не пишется никем. Сама
таблица, `AssetOrderBookCrud` и её ветка в ретеншне оставлены на месте:
решение выбрасывать накопленное принимает владелец, а не эта правка.

**Базовый URL.** `depth` относится к категории `/public` — Binance
2026-04-23 разнёс фьючерсные потоки по путям, и подписка на неверный
путь принимается молча, без единого фрейма данных (пункт 29 в
`.ai/docs/test-bots/08-gotchas.md`). Поэтому слушатель считает
пришедшие фреймы и ругается на молчание, а не на разрыв: «соединение
установлено» здесь ничего не доказывает.

Подписка идёт командой `SUBSCRIBE`, а не списком стримов в URL. Причина
прозаическая: в `watched_pair` есть пары с иероглифами в тикере
(`哈基米USDT`), и в URL их пришлось бы процентно кодировать — лишний
способ промахнуться.

Запуск (вручную; в `supervisord.conf` заведён с `autostart=false`):

    docker exec -it orderflow_general python -m app.scripts.order_book_listener

Архив пишется в `ORDER_BOOK_ARCHIVE_DIR`, который в docker-compose
смонтирован из `BINANCE_HISTORY_DIR` хоста, — то есть вне репозитория.
"""
import asyncio
import gzip
import json
import logging
import signal
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import websockets

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.crud.watched_pair import WatchedPairCrud
from app.dependencies import redis_context

# depth относится к категории /public — см. комментарий к WS_URL в
# watch_ws_and_save.py и пункт 29 в 08-gotchas.md.
WS_URL = "wss://fstream.binance.com/public/stream"
# Частоту задаёт ORDER_BOOK_STREAM_SPEED; Binance принимает 100ms, 250ms
# и 500ms.
STREAM_TEMPLATE = "@depth20@{speed}"
# Каталог контрактов для ORDER_BOOK_SYMBOLS=all. Только читаем.
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
# Длинную команду SUBSCRIBE Binance не примет, а на входящие команды у
# него лимит 5 в секунду — те же ограничения, что и у питателя цен.
SUBSCRIBE_CHUNK = 150
SUBSCRIBE_PAUSE_SEC = 0.3
# Переподключение: с чего начинать, докуда расти и какое соединение
# считать удачным. Пока рвёт сеть, а не биржа, длинная пауза только
# увеличивает дыру в данных.
RECONNECT_MIN_SEC = 0.5
RECONNECT_MAX_SEC = 8.0
RECONNECT_HEALTHY_SEC = 15.0
# Как часто сбрасывать буферы gzip на диск. Файл суточный, но ждать
# сутки, чтобы увидеть в нём строки, никто не станет.
FLUSH_INTERVAL_SEC = 10.0
# Как часто печатать, сколько фреймов пришло.
REPORT_INTERVAL_SEC = 60.0
EMPTY_SYMBOLS_RETRY_SECONDS = 30

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO
)


def _levels(raw, limit: int) -> list[list[str]]:
    """`[["цена", "объём"], ...]`, обрезанные до `limit`.

    Строками, а не Decimal: в архив они уходят как есть, а считать по
    ним будет офлайн-счёт — он и решит, какая точность ему нужна.
    """
    if not isinstance(raw, list):
        return []

    return [
        [str(level[0]), str(level[1])]
        for level in raw[:limit]
        if isinstance(level, (list, tuple)) and len(level) >= 2
    ]


def snapshot_from_payload(data: dict, limit: int) -> dict | None:
    """Снимок книги из фрейма `@depth20@100ms`.

    `None` — если в сообщении нет обеих сторон: такой снимок ни в архив,
    ни в Redis не нужен, по нему ничего не посчитать.
    """
    symbol = data.get("s")
    bids = _levels(data.get("b"), limit)
    asks = _levels(data.get("a"), limit)

    if not symbol or not bids or not asks:
        return None

    return {
        "symbol": symbol,
        # E — время события у биржи, T — время транзакции в матчинге.
        "event_time_ms": data.get("E"),
        "transaction_time_ms": data.get("T"),
        "bids": bids,
        "asks": asks,
    }


def order_book_imbalance(
    bids: list[list[str]], asks: list[list[str]], levels: int
) -> Decimal | None:
    """OBI по первым `levels` уровням: (bid − ask) / (bid + ask).

    От −1 (вся плотность в продавцах) до +1 (в покупателях). Считается по
    объёму, а не по числу заявок: объём — это то, что придётся пройти
    цене, а заявок может быть одна на весь уровень.

    `None` — если считать не по чему: пустая книга или нулевой объём с
    обеих сторон. Ноль в этом месте соврал бы «равновесие».
    """
    try:
        bid_volume = sum(Decimal(q) for _, q in bids[:levels])
        ask_volume = sum(Decimal(q) for _, q in asks[:levels])
    except (InvalidOperation, TypeError, ValueError):
        return None

    total = bid_volume + ask_volume

    if total <= 0:
        return None

    return (bid_volume - ask_volume) / total


def archive_path(root: Path, symbol: str, day: str) -> Path:
    """`<корень>/<СИМВОЛ>/<СИМВОЛ>-depth20-<ГГГГ-ММ-ДД>.jsonl.gz`.

    Структура повторяет раскладку data.binance.vision: офлайн-счёт и так
    ходит по ней, и лишнего формата ему знать не придётся.
    """
    return root / symbol / f"{symbol}-depth20-{day}.jsonl.gz"


def utc_day(event_ms: int | None = None) -> str:
    """Сутки по UTC — те же, по которым нарезан бакет Binance."""
    moment = (
        datetime.fromtimestamp(event_ms / 1000, tz=timezone.utc)
        if isinstance(event_ms, int)
        else datetime.now(tz=timezone.utc)
    )

    return moment.strftime("%Y-%m-%d")


class DailyArchive:
    """Суточные gzip-файлы, по одному на пару.

    Сутки берутся из времени события биржи, а не из часов машины: иначе
    на границе суток строки разъезжаются по файлам из-за задержки сети.
    """

    def __init__(self, root: Path):
        self.root = root
        self._handles: dict[tuple[str, str], gzip.GzipFile] = {}
        self.written = 0

    def write(self, snapshot: dict) -> None:
        symbol = snapshot["symbol"]
        day = utc_day(snapshot.get("event_time_ms"))
        key = (symbol, day)
        handle = self._handles.get(key)

        if handle is None:
            # Старые сутки той же пары закрываем сразу: держать открытым
            # файл, в который уже никто не пишет, незачем.
            for (other_symbol, other_day), other in list(self._handles.items()):
                if other_symbol == symbol:
                    other.close()
                    del self._handles[(other_symbol, other_day)]

            path = archive_path(self.root, symbol, day)
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = gzip.open(path, "at", encoding="utf-8")
            self._handles[key] = handle

        # Символ и так в имени файла, в строке его не повторяем.
        line = {
            "E": snapshot.get("event_time_ms"),
            "T": snapshot.get("transaction_time_ms"),
            "b": snapshot["bids"],
            "a": snapshot["asks"],
        }
        handle.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.written += 1

    def flush(self) -> None:
        for handle in self._handles.values():
            handle.flush()

    def close(self) -> None:
        for handle in self._handles.values():
            handle.close()

        self._handles.clear()


def split_streams(streams: list[str], per_socket: int) -> list[list[str]]:
    """Стримы по сокетам.

    Одно соединение на все пары упирается в потолок около двадцати
    сообщений в секунду и рвётся тем чаще, чем больше на нём стримов, —
    замер 2026-09-24 на плохом канале. Раскладка тех же стримов по
    отдельным сокетам возвращает и скорость, и устойчивость.

    `per_socket <= 0` — «все одним сокетом», прежнее поведение.
    """
    if per_socket <= 0 or not streams:
        return [streams] if streams else []

    return [
        streams[index:index + per_socket]
        for index in range(0, len(streams), per_socket)
    ]


async def _all_perpetual_symbols() -> list[str]:
    """Все торгуемые бессрочные контракты USDⓈ-M, из каталога биржи.

    Никуда не пишется: `watched_pair` этот режим не трогает, иначе
    сменился бы состав пар парка и обнулилась сопоставимость с
    накопленной статистикой.
    """
    while True:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(EXCHANGE_INFO_URL, timeout=60)
                response.raise_for_status()

            return sorted(
                item["symbol"] for item in response.json()["symbols"]
                if item.get("status") == "TRADING"
                and item.get("contractType") == "PERPETUAL"
            )
        except Exception as error:
            logging.info(
                f"Каталог контрактов не прочитался: {error}. Жду "
                f"{EMPTY_SYMBOLS_RETRY_SECONDS} с."
            )
            await asyncio.sleep(EMPTY_SYMBOLS_RETRY_SECONDS)


async def _watched_symbols() -> list[str]:
    """Пары из `watched_pair`. Ждёт, а не выходит: на пустом списке выйти
    означало бы FATAL у supervisord и мёртвый сбор после наполнения."""
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    while True:
        async with dsm.get_session() as session:
            symbols = sorted(
                (await WatchedPairCrud(session).get_symbol_to_id_map()).keys()
            )

        if symbols:
            return symbols

        logging.info(
            f"Подписываться не на что: watched_pair пуст. Жду "
            f"{EMPTY_SYMBOLS_RETRY_SECONDS} с."
        )
        await asyncio.sleep(EMPTY_SYMBOLS_RETRY_SECONDS)


async def _subscribe(ws, streams: list[str]) -> None:
    for index in range(0, len(streams), SUBSCRIBE_CHUNK):
        await ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": streams[index:index + SUBSCRIBE_CHUNK],
            "id": index // SUBSCRIBE_CHUNK + 1,
        }))
        await asyncio.sleep(SUBSCRIBE_PAUSE_SEC)


async def run_order_book_listener():
    levels = settings.ORDER_BOOK_LEVELS
    publish_interval = settings.ORDER_BOOK_PUBLISH_SEC
    archive_interval = settings.ORDER_BOOK_ARCHIVE_SEC
    ttl_ms = int(settings.ORDER_BOOK_REDIS_TTL_SEC * 1000)
    silence_limit = settings.ORDER_BOOK_SILENCE_LIMIT_SEC

    root = Path(settings.ORDER_BOOK_ARCHIVE_DIR).expanduser()
    root.mkdir(parents=True, exist_ok=True)

    source = (settings.ORDER_BOOK_SYMBOLS or "watched").strip().lower()

    if source == "all":
        symbols = await _all_perpetual_symbols()
    else:
        if source != "watched":
            logging.info(
                f"Неизвестный ORDER_BOOK_SYMBOLS={source!r}, беру watched."
            )

        symbols = await _watched_symbols()

    suffix = STREAM_TEMPLATE.format(speed=settings.ORDER_BOOK_STREAM_SPEED)
    streams = [f"{symbol.lower()}{suffix}" for symbol in symbols]
    groups = split_streams(streams, settings.ORDER_BOOK_STREAMS_PER_SOCKET)

    logging.info(
        f"Стакан: {len(symbols)} пар ({source}), {suffix}, уровней "
        f"{levels}, сокетов {len(groups)}. Redis раз в {publish_interval} с, "
        f"архив раз в {archive_interval} с в {root}."
    )

    # Последний снимок по каждой паре. Не очередь: и в Redis, и в архив
    # уходит состояние на момент такта, промежуточные никому не нужны.
    latest: dict[str, dict] = {}
    published_at: dict[str, int] = {}
    archived_at: dict[str, int] = {}
    archive = DailyArchive(root)

    # Счётчик фреймов — единственная честная проверка живости потока.
    # `last_ms` ведётся по каждому сокету отдельно: с одним общим
    # счётчиком замолчавший сокет прячется за живыми соседями.
    frames = {"total": 0}
    last_ms: dict[str, int] = {}

    async def reader(ws, label: str):
        while True:
            payload = json.loads(await ws.recv())
            data = payload.get("data") or {}
            snapshot = snapshot_from_payload(data, levels)

            if snapshot is None:
                continue

            frames["total"] += 1
            last_ms[label] = int(time.time() * 1000)
            latest[snapshot["symbol"]] = snapshot

    async def connection(group: list[str], label: str):
        # Пауза между попытками растёт только у соединений, которые
        # умерли сразу. Фиксированные три секунды на каждый обрыв — это
        # дорого там, где рвётся часто: замер в ночь на 2026-09-24 дал
        # 877 обрывов за 11 часов (плохой Wi-Fi, 8.11), то есть 44
        # минуты сна на ровном месте. Соединение, прожившее дольше
        # RECONNECT_HEALTHY_SEC, считается удачным, и следующая попытка
        # идёт почти сразу.
        pause = RECONNECT_MIN_SEC

        while True:
            opened_at = time.time()

            try:
                async with websockets.connect(
                    WS_URL, open_timeout=25, max_queue=None
                ) as ws:
                    await _subscribe(ws, group)
                    logging.info(f"{label}: подписано стримов {len(group)}.")
                    last_ms[label] = int(time.time() * 1000)
                    await reader(ws, label)
            except websockets.exceptions.ConnectionClosedOK:
                logging.info(f"⚠️ {label}: соединение закрыто. Переподключаюсь...")
            except websockets.exceptions.ConnectionClosedError as e:
                logging.info(f"❌ {label}: соединение оборвано: {e}.")
            except Exception as e:
                logging.info(f"❌ {label}: ошибка: {e}.")
            finally:
                if time.time() - opened_at >= RECONNECT_HEALTHY_SEC:
                    pause = RECONNECT_MIN_SEC
                else:
                    pause = min(pause * 2, RECONNECT_MAX_SEC)

                await asyncio.sleep(pause)

    async def watchdog():
        """Молчащий сокет — отказ, а не покой.

        Так выглядит неверный базовый URL: подписка подтверждена, PING
        отвечает, данных нет. Ждать такого молча — значит повторить те
        полтора месяца, которые описаны в пункте 29 08-gotchas.md.
        """
        while True:
            await asyncio.sleep(silence_limit)
            now_ms = int(time.time() * 1000)
            silent = [
                label for label, seen in last_ms.items()
                if seen and now_ms - seen >= silence_limit * 1000
            ]

            if silent:
                logging.error(
                    f"❌ Молчат при подтверждённой подписке: "
                    f"{', '.join(sorted(silent))} — дольше "
                    f"{int(silence_limit)} с. Сверить базовый URL "
                    f"({WS_URL}): depth живёт на /public."
                )

    async def publisher():
        while True:
            await asyncio.sleep(publish_interval)

            fresh = [
                snapshot for symbol, snapshot in list(latest.items())
                if isinstance(snapshot.get("event_time_ms"), int)
                and snapshot["event_time_ms"] > published_at.get(symbol, 0)
            ]

            if not fresh:
                continue

            async with redis.pipeline(transaction=False) as pipe:
                for snapshot in fresh:
                    pipe.set(
                        f"order_book:{snapshot['symbol']}",
                        json.dumps(snapshot, separators=(",", ":")),
                        px=ttl_ms,
                    )
                await pipe.execute()

            for snapshot in fresh:
                published_at[snapshot["symbol"]] = snapshot["event_time_ms"]

    async def historian():
        while True:
            await asyncio.sleep(max(archive_interval, 0.05))

            for symbol, snapshot in list(latest.items()):
                event_ms = snapshot.get("event_time_ms")

                if not isinstance(event_ms, int):
                    continue

                if event_ms <= archived_at.get(symbol, 0):
                    continue

                archive.write(snapshot)
                archived_at[symbol] = event_ms

    async def flusher():
        while True:
            await asyncio.sleep(FLUSH_INTERVAL_SEC)
            archive.flush()

    async def reporter():
        previous = 0

        while True:
            await asyncio.sleep(REPORT_INTERVAL_SEC)
            received = frames["total"] - previous
            previous = frames["total"]
            logging.info(
                f"Фреймов за {int(REPORT_INTERVAL_SEC)} с: {received} "
                f"({received / REPORT_INTERVAL_SEC:.0f}/с), "
                f"строк в архив всего: {archive.written}."
            )

    # Закрыть gzip по сигналу, а не когда получится. Без этого у каждого
    # суточного файла не дописывается хвост, и `zcat` на нём ругается:
    # данные читаются, но поверх каждой проверки идёт «format violated».
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()

    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, stopping.set)

    # Один Redis на весь процесс, а не `redis_context()` на каждом такте.
    # `RedisSessionManager` отдаёт клиента через `Redis.from_pool`, а тот
    # на закрытии рвёт **весь** пул — общий, singleton по адресу. Здесь
    # публикатор один и сам с собой не гонится, но переподключаться
    # десять раз в секунду незачем; у питателя котировок тот же приём
    # лечит падения процесса, см. комментарий к `publisher` в
    # `watch_ws_and_save.py`.
    async with redis_context() as redis:
        work = asyncio.gather(
            *(connection(group, f"сокет {number}")
              for number, group in enumerate(groups, start=1)),
            watchdog(), publisher(), historian(), flusher(), reporter(),
        )

        try:
            await asyncio.wait(
                [work, asyncio.create_task(stopping.wait())],
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            work.cancel()
            # Дождаться отмены, а не просто попросить: иначе на выходе
            # остаются незавершённые задачи и asyncio ругается поверх итога.
            await asyncio.gather(work, return_exceptions=True)
            archive.close()
            logging.info(
                f"Стакан остановлен. Фреймов принято {frames['total']}, "
                f"строк в архив {archive.written}."
            )


if __name__ == "__main__":
    asyncio.run(run_order_book_listener())
