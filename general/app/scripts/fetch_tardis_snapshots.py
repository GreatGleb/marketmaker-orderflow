"""Выгрузка бесплатных дней Tardis.dev: стакан фьючерсов Binance по 25 уровней.

Запускается руками, в `supervisord.conf` не добавляется, как и
`fetch_binance_history.py`, — и рассчитан так же на запуск **с хоста**:
только httpx и стандартная библиотека.

Зачем. Свой архив `@depth20` (`order_book_listener.py`) пишет снимок раз
в секунду и копится днями, а окну признаков в 500 мс нужна частота
выше (раздел 14.4 плана `.ai/docs/test-bots/15-orderflow-plan.md`).
Tardis отдаёт `book_snapshot_25` — снимок на **каждое** изменение книги,
с микросекундами, — и за первое число каждого месяца без ключа.
Со второго числа нужен ключ (401).

Что важно про данные:

* снимок пишется на изменение книги, поэтому долгая пауза между
  снимками значит «книга не менялась», а не обрыв, — в отличие от
  своего архива;
* пара, которой в этот день ещё не было, отвечает 400 с кодом 140.
  План строится заранее по `availableSince`/`availableTo` из
  `EXCHANGE_URL`, и такие дни в план не попадают вовсе;
* у ответа есть `x-md5` и `content-length`. Сумма записывается рядом
  с архивом (`.md5`) и сверяется при скачивании и в `--verify`.

Куда кладёт: `BINANCE_HISTORY_DIR/tardis/`, раскладкой их URL —
`binance-futures/book_snapshot_25/ГГГГ/ММ/ДД/СИМВОЛ.csv.gz`. Вне
репозитория, как и остальная история.

    cd general
    BINANCE_HISTORY_DIR=~/binance-history python3 -m app.scripts.fetch_tardis_snapshots \\
        --since 2024-10-01 --until 2026-09-01 --symbols-file symbols.txt
    ... посмотреть план ...
    ... тот же запуск с --download, потом с --verify

`--verify` распаковывает каждый архив целиком: сверяет md5, проверяет
gzip, считает строки и берёт первую и последнюю метку времени. Итог —
`tardis/verify_book_snapshot_25.csv`, по строке на день пары.
"""
import argparse
import asyncio
import csv
import hashlib
import json
import os
import sys
import zlib
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

EXCHANGE = "binance-futures"
DATA_TYPE = "book_snapshot_25"
DATASET_URL = "https://datasets.tardis.dev/v1/{exchange}/{kind}/{day:%Y/%m/%d}/{symbol}.csv.gz"
EXCHANGE_URL = "https://api.tardis.dev/v1/exchanges/{exchange}"
DEFAULT_HISTORY_DIR = "~/binance-history"

DEFAULT_JOBS = 4
# Бесплатный доступ ограничен объёмом: около 47 ГБ, дальше 429 с кодом 277
# и `retryAfterSeconds` порядка суток. Повторять такой ответ бессмысленно.
QUOTA_CODE = 277


class QuotaExceeded(Exception):
    pass
RETRIES = 4
CHUNK_BYTES = 1 << 20


def first_days(since: date, until: date) -> list[date]:
    """Первые числа месяцев в [since, until] — бесплатные дни Tardis."""
    days = []
    year, month = since.year, since.month

    if since.day != 1:
        month += 1

    while True:
        if month > 12:
            year, month = year + 1, 1

        day = date(year, month, 1)

        if day > until:
            return days

        days.append(day)
        month += 1


def local_path(root: Path, symbol: str, day: date) -> Path:
    return (root / "tardis" / EXCHANGE / DATA_TYPE / f"{day:%Y/%m/%d}"
            / f"{symbol}.csv.gz")


def available(info: dict, day: date) -> bool:
    since = info.get("availableSince", "")[:10]
    until = info.get("availableTo", "9999-12-31")[:10]
    # availableTo — исключительная граница: момент, до которого данные есть.
    return since <= day.isoformat() < until


def md5_of(path: Path) -> str:
    digest = hashlib.md5()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)

    return digest.hexdigest()


def done(path: Path) -> bool:
    """Архив лежит и сходится с суммой, записанной при скачивании."""
    checksum = path.with_suffix(path.suffix + ".md5")

    if not path.exists() or not checksum.exists():
        return False

    return md5_of(path) == checksum.read_text().strip()


async def download(client: httpx.AsyncClient, url: str, target: Path) -> str:
    """Качает через `.part`; возвращает `ok` или текст ошибки."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    error = ""

    for attempt in range(1, RETRIES + 1):
        try:
            digest = hashlib.md5()
            size = 0

            async with client.stream("GET", url, timeout=600) as response:
                if response.status_code == 400:
                    body = (await response.aread()).decode(errors="replace")
                    return f"400: {body[:200]}"

                if response.status_code == 429:
                    body = (await response.aread()).decode(errors="replace")

                    try:
                        payload = json.loads(body)
                    except ValueError:
                        payload = {}

                    if payload.get("code") == QUOTA_CODE:
                        raise QuotaExceeded(
                            f"лимит объёма, повтор через "
                            f"{payload.get('retryAfterSeconds')} с"
                        )

                response.raise_for_status()
                wanted_md5 = response.headers.get("x-md5", "").strip('"')
                wanted_size = int(response.headers.get("content-length", -1))

                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes(CHUNK_BYTES):
                        handle.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)

            if wanted_size >= 0 and size != wanted_size:
                raise ValueError(f"размер {size} вместо {wanted_size}")

            if not wanted_md5:
                raise ValueError("в ответе нет x-md5, сверить нечем")

            if digest.hexdigest() != wanted_md5:
                raise ValueError("md5 не сошёлся")

            temporary.replace(target)
            target.with_suffix(target.suffix + ".md5").write_text(wanted_md5)
            return "ok"
        except QuotaExceeded:
            temporary.unlink(missing_ok=True)
            raise
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            error = str(exc) or type(exc).__name__
            await asyncio.sleep(3 * attempt)

    return error


def verify_one(task) -> dict:
    """Полная проверка архива: md5, gzip, строки, первая и последняя метка."""
    symbol, day, path = task
    row = {"symbol": symbol, "day": day, "status": "", "bytes": 0,
           "rows": 0, "first_ts": "", "last_ts": "",
           "starts_after_s": "", "ends_before_s": ""}
    path = Path(path)

    if not path.exists():
        row["status"] = "нет файла"
        return row

    row["bytes"] = path.stat().st_size

    if not done(path):
        row["status"] = "md5 не сошёлся"
        return row

    decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
    lines = 0
    head = b""
    tail = b""

    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
                data = decompressor.decompress(chunk)

                if len(head) < 4096:
                    head += data[:4096]

                lines += data.count(b"\n")
                tail = (tail + data)[-4096:]

        rest = decompressor.flush()
        lines += rest.count(b"\n")
        tail = (tail + rest)[-4096:]

        if not decompressor.eof:
            row["status"] = "gzip оборван"
            return row
    except zlib.error as exc:
        row["status"] = f"gzip: {exc}"
        return row

    body = head.split(b"\n")
    last = [line for line in tail.split(b"\n") if line][-1]
    rows = lines - 1

    if rows <= 0 or len(body) < 2:
        row["status"] = "пусто"
        return row

    # Третья колонка — timestamp биржи в микросекундах.
    first_us = int(body[1].split(b",")[2])
    last_us = int(last.split(b",")[2])
    day_start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    start_us = int(day_start.timestamp() * 1_000_000)

    row.update(
        status="ok", rows=rows,
        first_ts=datetime.fromtimestamp(first_us / 1e6, timezone.utc).isoformat(),
        last_ts=datetime.fromtimestamp(last_us / 1e6, timezone.utc).isoformat(),
        starts_after_s=round((first_us - start_us) / 1e6, 1),
        ends_before_s=round((start_us + 86_400_000_000 - last_us) / 1e6, 1),
    )
    return row


async def fetch_catalog() -> dict[str, dict]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        response = await client.get(EXCHANGE_URL.format(exchange=EXCHANGE),
                                    timeout=120)
        response.raise_for_status()

    return {s["id"]: s for s in response.json()["datasets"]["symbols"]}


def load_symbols(args) -> list[str]:
    if args.symbols_file:
        text = Path(args.symbols_file).expanduser().read_text()
    else:
        text = args.symbols or ""

    return [s.strip() for s in text.replace("\n", ",").split(",") if s.strip()]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, type=date.fromisoformat)
    parser.add_argument("--until", required=True, type=date.fromisoformat)
    parser.add_argument("--symbols", help="через запятую")
    parser.add_argument("--symbols-file", help="файл со списком через запятую")
    parser.add_argument(
        "--pairs-file",
        help="CSV с колонками day,symbol: качать только эти дни пар "
             "(пересекается с планом по --since/--until)",
    )
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--verify", action="store_true")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR)
                ).expanduser()
    symbols = load_symbols(args)
    days = first_days(args.since, args.until)
    catalog = await fetch_catalog()

    unknown = [s for s in symbols if s not in catalog]
    plan = [(s, d) for s in symbols if s in catalog for d in days
            if available(catalog[s], d)]

    if args.pairs_file:
        with Path(args.pairs_file).expanduser().open() as handle:
            wanted = {(r["symbol"], date.fromisoformat(r["day"]))
                      for r in csv.DictReader(handle)}
        plan = [item for item in plan if item in wanted]
    print(f"Дней: {len(days)} ({days[0]} .. {days[-1]}), пар: {len(symbols)}, "
          f"дней пар в плане: {len(plan)}. Каталог: {root / 'tardis'}")

    if unknown:
        print(f"Нет у Tardis ({len(unknown)}): {', '.join(unknown[:20])}")

    by_day = {d: sum(1 for _, x in plan if x == d) for d in days}
    print("Пар по дням: " + ", ".join(f"{d:%Y-%m} {n}" for d, n in by_day.items()))

    if args.verify:
        tasks = [(s, d.isoformat(), str(local_path(root, s, d)))
                 for s, d in plan]
        out = root / "tardis" / f"verify_{DATA_TYPE}.csv"

        with ProcessPoolExecutor(args.jobs) as pool:
            rows = list(pool.map(verify_one, tasks, chunksize=4))

        with out.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

        bad = [r for r in rows if r["status"] != "ok"]
        total = sum(r["bytes"] for r in rows)
        print(f"\nПроверено {len(rows)}, в порядке {len(rows) - len(bad)}, "
              f"с ошибкой {len(bad)}, объём {total / (1 << 30):.1f} ГБ. "
              f"Подробно: {out}")

        for r in bad[:30]:
            print(f"  {r['day']} {r['symbol']}: {r['status']}")

        return 1 if bad else 0

    if not args.download:
        print("\nЭто только план. Качать: тот же запуск с --download.")
        return 0

    semaphore = asyncio.Semaphore(args.jobs)
    results: dict[str, list[str]] = {"ok": [], "уже было": [], "сбой": []}
    failures: list[str] = []
    progress = 0

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def one(symbol: str, day: date) -> None:
            nonlocal progress

            async with semaphore:
                target = local_path(root, symbol, day)

                if done(target):
                    results["уже было"].append(symbol)
                else:
                    url = DATASET_URL.format(
                        exchange=EXCHANGE, kind=DATA_TYPE, day=day,
                        symbol=quote(symbol),
                    )
                    status = await download(client, url, target)

                    if status == "ok":
                        results["ok"].append(symbol)
                    else:
                        results["сбой"].append(symbol)
                        failures.append(f"{day} {symbol}: {status}")

                progress += 1

                if progress % 200 == 0:
                    print(f"  {progress}/{len(plan)}, сбоев {len(failures)}",
                          flush=True)

        # Старые дни первыми: у них меньше пар, и ошибка схемы вылезет
        # до того, как скачаны сотни гигабайт.
        try:
            await asyncio.gather(*(one(s, d) for s, d in sorted(
                plan, key=lambda item: (item[1], item[0]))))
        except QuotaExceeded as exc:
            print(f"\nОстановлено: {exc}. Скачанное цело, повторный "
                  f"запуск продолжит с места остановки.", flush=True)
            return 2

    print(f"\nСкачано {len(results['ok'])}, уже лежало "
          f"{len(results['уже было'])}, сбоев {len(results['сбой'])}.")

    for line in failures[:50]:
        print(f"  {line}")

    report = root / "tardis" / f"failures_{DATA_TYPE}.json"
    report.write_text(json.dumps(failures, ensure_ascii=False, indent=1))

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
