"""Выгрузка публичной истории фьючерсов Binance (data.binance.vision).

Запускается руками, в `supervisord.conf` не добавляется: это разовая
подготовка данных для офлайн-счёта порогов (раздел 5.1 в
`.ai/docs/test-bots/15-orderflow-plan.md`), а не рантайм.

Что качаем и почему именно это:

* `aggTrades` — основной набор. Цена, объём и `isBuyerMaker` каждой
  сделки: из него собирается и секундная сетка, и дельта, и CVD;
* `klines/1m` — ATR для нормировки импульса. Секундных свечей на
  фьючерсах не существует вовсе (минимум 1m), секунды строятся из
  `aggTrades`;
* `metrics` — открытый интерес и `takerBuySellVolume`, шаг 5 минут. Фон,
  не триггер.

`bookTicker` в списке нет намеренно: публикация прекращена, последний
день — 2024-03-30, дальше 404. Спред и OBI по фьючерсам скачать неоткуда,
они собираются своим слушателем (`order_book_listener.py`, раздел 5.3).

Список пар берётся из `watched_pair` восстановленной базы, а не из
головы: это низколиквидные альты, часть из них листнули позже нужного
месяца. Посчитать пороги по BTC и перенести на них — ровно та ошибка,
ради которой пары и сверяются с каталогом; выпавшие печатаются отдельным
списком.

Каталог читается листингом бакета (`?prefix=...`), а не перебором дат:
один запрос на пару вместо тридцати одного, и в ответе сразу точные
размеры — поэтому объём известен до скачивания, а не после.

Куда кладёт: `BINANCE_HISTORY_DIR` (по умолчанию `~/binance-history`),
структурой бакета. Вне репозитория — в git не должно попасть ничего.

Зависимости специально узкие: httpx и sqlalchemy, без `app.db.base` и без
`app.config`. Скрипт рассчитан на запуск **с хоста** — каталог выгрузки
лежит вне контейнера, — а на хосте оба импорта не работают: `app.db.base`
тянет `redis`, а `Settings` спотыкается о корневой `.env` с `REDIS_PORT`
(он предназначен для подстановки в `docker-compose.yml`, а не для
настроек приложения). Поэтому адрес базы — свой аргумент, и по умолчанию
он смотрит на опубликованный порт Postgres, а не на имя сервиса `db`:

    cd general
    BINANCE_HISTORY_DIR=~/binance-history python3 -m app.scripts.fetch_binance_history
    ... посмотреть объём ...
    BINANCE_HISTORY_DIR=~/binance-history python3 -m app.scripts.fetch_binance_history --download

Без `--download` скрипт только считает объём и печатает план. Так и
задумано: месяц на полусотне пар — это гигабайты, и решение качать
принимает человек.
"""
import argparse
import asyncio
import calendar
import hashlib
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

BASE_URL = "https://data.binance.vision"
# Каталог фьючерсных контрактов. Нужен для `--symbols all`: бакет
# перечислить можно, но в нём лежат и давно делистнутые пары, а торгуются
# не все из них.
EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
# Листинг отдаёт только S3-эндпоинт бакета; сам data.binance.vision — это
# CDN поверх него и на ?prefix= не отвечает.
BUCKET_URL = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
XMLNS = "{http://s3.amazonaws.com/doc/2006-03-01/}"

# Префикс в бакете -> человеческое имя набора. Порядок важен только для
# вывода: aggTrades первый, потому что он же и самый тяжёлый.
DATASETS = {
    "aggTrades": "data/futures/um/daily/aggTrades/{symbol}/",
    "klines1m": "data/futures/um/daily/klines/{symbol}/1m/",
    "metrics": "data/futures/um/daily/metrics/{symbol}/",
}

# Как называется файл между символом и датой: у klines это интервал, а
# не имя набора. Нужно, чтобы сузить листинг до месяца одним префиксом —
# см. `month_prefix`.
DATASET_FILE_INFIX = {
    "aggTrades": "aggTrades",
    "klines1m": "1m",
    "metrics": "metrics",
}

# Пары из watched_pair, как они попадают в парк. Тот же запрос, что в
# постановке: `watched_pair` хранит ссылку, символ живёт в спецификациях.
WATCHED_PAIRS_SQL = """
select s.symbol
from watched_pair w
join asset_exchange_specs s on s.id = w.asset_exchange_id
order by 1
"""

DEFAULT_HISTORY_DIR = "~/binance-history"
# Postgres стека, как он виден с хоста: порт из docker-compose опубликован,
# а имя сервиса `db` снаружи сети не разрешается.
DEFAULT_DB_URL = "postgresql+asyncpg://postgres:secret@localhost:5432/postgres"
# Вежливость к бакету: он публичный и бесплатный, выжимать из него всё
# незачем. Четыре потока с паузой — это около 20 запросов в секунду.
DEFAULT_JOBS = 4
DEFAULT_PAUSE_SEC = 0.2
# Порог, выше которого скрипт отказывается качать без явного разрешения.
# Десятки гигабайт на ноутбуке — это разговор, а не мелочь.
DEFAULT_MAX_GB = 40.0
# Сколько раз пробовать один файл: бакет изредка рвёт соединение.
RETRIES = 3
CHUNK_BYTES = 1 << 20


def last_full_month(today: date) -> tuple[date, date]:
    """Последний полный месяц. Файл за текущие сутки появляется только на
    следующий день, поэтому «этот месяц» не бывает полным никогда."""
    first_of_this = today.replace(day=1)
    last_day = first_of_this - timedelta(days=1)

    return last_day.replace(day=1), last_day


def month_bounds(value: str) -> tuple[date, date]:
    year, month = (int(part) for part in value.split("-"))
    days = calendar.monthrange(year, month)[1]

    return date(year, month, 1), date(year, month, days)


def human_bytes(size: float) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.1f} {unit}"
        size /= 1024


def date_from_key(key: str) -> date | None:
    """Дата из имени файла: `...-2026-08-31.zip`.

    По имени, а не по `LastModified`: файл за 31 августа выкладывают
    первого сентября, и по времени выкладки месяц съезжает на день.
    """
    stem = key.rsplit("/", 1)[-1]

    if not stem.endswith(".zip"):
        return None

    try:
        year, month, day = stem[:-4].rsplit("-", 3)[-3:]
        return date(int(year), int(month), int(day))
    except ValueError:
        return None


async def all_futures_symbols(client: httpx.AsyncClient) -> list[str]:
    """Все торгуемые бессрочные контракты USDⓈ-M.

    Бессрочные и только `TRADING`: у квартальных другой характер
    ликвидности, а делистнутые пары дадут обрывки месяца и перекосят
    объединённую статистику.
    """
    response = await client.get(EXCHANGE_INFO_URL, timeout=60)
    response.raise_for_status()

    return sorted(
        item["symbol"] for item in response.json()["symbols"]
        if item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
    )


async def watched_symbols(db_url: str) -> list[str]:
    engine = create_async_engine(db_url, connect_args={"statement_cache_size": 0})

    try:
        async with engine.connect() as connection:
            result = await connection.execute(text(WATCHED_PAIRS_SQL))
            return [row[0] for row in result]
    finally:
        await engine.dispose()


async def list_prefix(
    client: httpx.AsyncClient, prefix: str, name_start: str = "",
    month: str = "",
) -> dict[str, int]:
    """Все объекты под префиксом: ключ -> размер. Со страничками.

    Бакет отдаёт максимум 1000 ключей за раз, а у давно листнутой пары
    их вдвое больше числа дней — рядом с каждым архивом лежит `.CHECKSUM`.
    `month` сужает выдачу до одного месяца, см. `month_prefix`: имя файла
    всегда начинается с символа, поэтому дату можно дописать прямо в
    префикс — `.../ZESTUSDT/ZESTUSDT-aggTrades-2026-08`.
    """
    files: dict[str, int] = {}
    marker = ""
    while True:
        params = {"delimiter": "/", "prefix": prefix + name_start + month}

        if marker:
            params["marker"] = marker

        # Листинг — такой же сетевой запрос, как скачивание, и на
        # сотнях пар с постраничной выдачей один таймаут неизбежен.
        # Без повтора он ронял весь прогон на середине каталога.
        for attempt in range(1, RETRIES + 1):
            try:
                response = await client.get(
                    BUCKET_URL, params=params, timeout=60
                )
                response.raise_for_status()
                break
            except Exception:
                if attempt == RETRIES:
                    raise

                await asyncio.sleep(2 * attempt)

        tree = ElementTree.fromstring(response.content)

        last_key = ""
        for node in tree.findall(f"{XMLNS}Contents"):
            last_key = node.findtext(f"{XMLNS}Key") or ""
            files[last_key] = int(node.findtext(f"{XMLNS}Size") or 0)

        truncated = (tree.findtext(f"{XMLNS}IsTruncated") or "").lower() == "true"

        if not truncated or not last_key:
            return files

        marker = last_key


class Plan:
    """Что качать по одному набору одной пары."""

    def __init__(self, symbol: str, dataset: str):
        self.symbol = symbol
        self.dataset = dataset
        # Каталога нет вовсе — пары на фьючерсах не было никогда.
        self.catalog_empty = False
        # Первый и последний день, которые бакет знает по этой паре.
        self.first_day: date | None = None
        self.last_day: date | None = None
        self.files: list[tuple[str, int]] = []

    @property
    def total_bytes(self) -> int:
        return sum(size for _, size in self.files)


def month_prefix(since: date, until: date) -> str:
    """Хвост префикса, сужающий листинг до одного месяца.

    Без него листинг отдаёт **всю** историю пары: у контрактов с 2019
    года это тысячи ключей и десятки страниц на каждую, а нужен один
    месяц. С ним — одна страница. На 570 парах это разница между
    получасом и минутой.

    Пустая строка, если период шире месяца: тогда сужать нечем.
    """
    if (since.year, since.month) != (until.year, until.month):
        return ""

    return f"{since:%Y-%m}"


async def build_plan(
    client: httpx.AsyncClient, symbol: str, dataset: str,
    since: date, until: date, pause: float,
) -> Plan:
    plan = Plan(symbol, dataset)
    month = month_prefix(since, until)
    catalog = await list_prefix(
        client, DATASETS[dataset].format(symbol=symbol),
        f"{symbol}-{DATASET_FILE_INFIX[dataset]}-" if month else "", month,
    )
    await asyncio.sleep(pause)

    days = sorted(
        day for day in (date_from_key(key) for key in catalog) if day is not None
    )

    if not days:
        plan.catalog_empty = True
        return plan

    plan.first_day, plan.last_day = days[0], days[-1]

    for key, size in sorted(catalog.items()):
        day = date_from_key(key)

        if day is None or not since <= day <= until:
            continue

        plan.files.append((key, size))
        # Контрольная сумма лежит отдельным объектом и в план входит тоже:
        # без неё проверять нечем, а весит она сотню байт.
        checksum_key = f"{key}.CHECKSUM"

        if checksum_key in catalog:
            plan.files.append((checksum_key, catalog[checksum_key]))

    return plan


def local_path(root: Path, key: str) -> Path:
    return root / key


async def download(
    client: httpx.AsyncClient, root: Path, key: str, pause: float,
) -> None:
    target = local_path(root, key)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")

    for attempt in range(1, RETRIES + 1):
        try:
            async with client.stream(
                "GET", f"{BASE_URL}/{key}", timeout=300
            ) as response:
                response.raise_for_status()

                with temporary.open("wb") as handle:
                    async for chunk in response.aiter_bytes(CHUNK_BYTES):
                        handle.write(chunk)

            temporary.replace(target)
            await asyncio.sleep(pause)
            return
        except Exception:
            temporary.unlink(missing_ok=True)

            if attempt == RETRIES:
                raise

            await asyncio.sleep(2 * attempt)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)

    return digest.hexdigest()


def expected_sha256(checksum_path: Path) -> str | None:
    """`.CHECKSUM` — это строка «<sha256>  <имя файла>»."""
    try:
        first = checksum_path.read_text().split()
        return first[0].lower() if first else None
    except OSError:
        return None


def verified(root: Path, key: str) -> bool:
    """Архив уже лежит и сошёлся по контрольной сумме.

    Это и есть возобновляемость: повторный запуск не качает заново то,
    что скачано целиком. Битый или недокачанный файл сумму не пройдёт и
    будет скачан ещё раз.
    """
    archive = local_path(root, key)
    checksum = local_path(root, f"{key}.CHECKSUM")

    if not archive.exists() or not checksum.exists():
        return False

    wanted = expected_sha256(checksum)

    return bool(wanted) and sha256_of(archive) == wanted


async def fetch_dataset(
    client: httpx.AsyncClient, root: Path, plan: Plan, pause: float,
    semaphore: asyncio.Semaphore,
) -> tuple[int, int, list[str]]:
    """Возвращает (скачано дней, пропущено как готовые, список сбоев)."""
    archives = [key for key, _ in plan.files if not key.endswith(".CHECKSUM")]
    fetched = skipped = 0
    failures: list[str] = []

    async def one(key: str) -> None:
        nonlocal fetched, skipped

        async with semaphore:
            if verified(root, key):
                skipped += 1
                return

            try:
                # Сумма первой: скачивать архив, который нечем проверить,
                # незачем.
                await download(client, root, f"{key}.CHECKSUM", pause)
                await download(client, root, key, pause)
            except Exception as error:
                failures.append(f"{key}: {error}")
                return

            if verified(root, key):
                fetched += 1
            else:
                local_path(root, key).unlink(missing_ok=True)
                failures.append(f"{key}: контрольная сумма не сошлась")

    await asyncio.gather(*(one(key) for key in archives))

    return fetched, skipped, failures


def print_plan(plans, since: date, until: date, datasets) -> int:
    """Объём и состав выгрузки. Печатается всегда, качается — по флагу.

    Построчно пары показываются только пока их немного: на девятистах
    контрактах таблица на экран не влезает и решение принимать по ней
    нельзя, а нужны как раз итоги и списки выпавших.
    """
    total = 0
    missing: list[str] = []
    partial: list[tuple[str, int, date | None]] = []
    expected = (until - since).days + 1
    per_dataset = {dataset: [0, 0] for dataset in datasets}
    detailed = len(plans) <= 80

    print(f"\nПериод: {since} .. {until} ({expected} дней)\n")

    if detailed:
        header = f"{'символ':<18}" + "".join(f"{d:>16}" for d in datasets)
        print(header)

    for symbol, by_dataset in plans.items():
        columns = []

        for dataset in datasets:
            plan = by_dataset[dataset]
            archives = [k for k, _ in plan.files if not k.endswith(".CHECKSUM")]
            total += plan.total_bytes
            per_dataset[dataset][0] += len(archives)
            per_dataset[dataset][1] += plan.total_bytes
            columns.append(
                f"{len(archives):>3} дн {human_bytes(plan.total_bytes):>9}"
            )

        first = by_dataset[datasets[0]]
        days = len([k for k, _ in first.files if not k.endswith(".CHECKSUM")])

        if first.catalog_empty:
            missing.append(symbol)
        elif days < expected:
            partial.append((symbol, days, first.first_day))

        if detailed:
            print(f"{symbol:<18}" + "".join(f"{c:>16}" for c in columns))

    print(f"\nПар: {len(plans)}")

    for dataset, (archives, size) in per_dataset.items():
        print(f"  {dataset:<12} архивов {archives:>6}  {human_bytes(size):>10}")

    print(f"\nВсего к выгрузке: {human_bytes(total)}")

    if missing:
        shown = ", ".join(missing[:15]) + ("..." if len(missing) > 15 else "")
        print(f"\nНет в бакете вовсе ({len(missing)}): {shown}")

    if partial:
        print(f"\nНеполный месяц ({len(partial)}):")
        for symbol, days, first_day in partial[:20]:
            since_note = f", первый день в бакете {first_day}" if first_day else ""
            print(f"  {symbol}: {days} из {expected} дней{since_note}")

        if len(partial) > 20:
            print(f"  ... и ещё {len(partial) - 20}")

    return total


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Выгрузка истории фьючерсов с data.binance.vision"
    )
    parser.add_argument(
        "--month", help="месяц YYYY-MM; по умолчанию последний полный"
    )
    parser.add_argument(
        "--symbols",
        help="список через запятую вместо watched_pair; `all` — все "
             "торгуемые бессрочные контракты USDⓈ-M",
    )
    parser.add_argument(
        "--datasets", default=",".join(DATASETS),
        help=f"какие наборы качать, через запятую. Известны: "
             f"{', '.join(DATASETS)}",
    )
    parser.add_argument(
        "--download", action="store_true",
        help="качать. Без флага скрипт только считает объём и печатает план",
    )
    parser.add_argument(
        "--db-url", default=os.environ.get("DB_URL", DEFAULT_DB_URL),
        help="откуда читать watched_pair; по умолчанию стек на localhost",
    )
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS)
    parser.add_argument("--pause", type=float, default=DEFAULT_PAUSE_SEC)
    parser.add_argument(
        "--max-gb", type=float, default=DEFAULT_MAX_GB,
        help="потолок объёма, выше которого скрипт качать отказывается",
    )
    args = parser.parse_args()

    since, until = (
        month_bounds(args.month) if args.month else last_full_month(date.today())
    )

    root = Path(
        os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR)
    ).expanduser()

    datasets = [d.strip() for d in args.datasets.split(",") if d.strip()]
    unknown = sorted(set(datasets) - set(DATASETS))

    if unknown:
        print(f"Неизвестные наборы: {', '.join(unknown)}. "
              f"Известны: {', '.join(DATASETS)}.")
        return 1

    if args.symbols and args.symbols.strip().lower() == "all":
        async with httpx.AsyncClient(follow_redirects=True) as catalog_client:
            symbols = await all_futures_symbols(catalog_client)
    elif args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        symbols = await watched_symbols(args.db_url)

    if not symbols:
        print("В watched_pair пусто — качать нечего.")
        return 1

    print(f"Пар: {len(symbols)}. Наборы: {', '.join(datasets)}. "
          f"Каталог выгрузки: {root}")
    print("Читаю каталог бакета...")

    semaphore = asyncio.Semaphore(args.jobs)
    plans: dict[str, dict[str, Plan]] = {}

    async with httpx.AsyncClient(follow_redirects=True) as client:
        catalog_failures: list[str] = []

        async def catalog(symbol: str, dataset: str) -> None:
            async with semaphore:
                try:
                    plan = await build_plan(
                        client, symbol, dataset, since, until, args.pause
                    )
                except Exception as error:
                    # Пара, чей каталог не прочитался, не должна ронять
                    # прогон по остальным девятистам: её честнее
                    # показать отдельным списком.
                    catalog_failures.append(f"{symbol}/{dataset}: {error}")
                    plan = Plan(symbol, dataset)
                    plan.catalog_empty = True

                plans.setdefault(symbol, {})[dataset] = plan

        await asyncio.gather(*(
            catalog(symbol, dataset)
            for symbol in symbols for dataset in datasets
        ))

        plans = {symbol: plans[symbol] for symbol in symbols}
        total = print_plan(plans, since, until, datasets)

        if catalog_failures:
            print(f"\nКаталог не прочитался ({len(catalog_failures)}):")
            for line in catalog_failures[:10]:
                print(f"  {line}")

        if not args.download:
            print(
                "\nЭто только оценка. Качать: тот же запуск с --download."
            )
            return 0

        if total > args.max_gb * (1 << 30):
            print(
                f"\nОстановлено: {human_bytes(total)} больше потолка "
                f"{args.max_gb} ГБ. Урезать период (--month), список пар "
                f"(--symbols) или поднять потолок (--max-gb)."
            )
            return 1

        print("\nКачаю...", flush=True)
        report: dict[str, dict[str, tuple[int, int]]] = {}
        all_failures: list[str] = []

        for symbol in symbols:
            report[symbol] = {}

            for dataset in datasets:
                fetched, skipped, failures = await fetch_dataset(
                    client, root, plans[symbol][dataset], args.pause, semaphore
                )
                report[symbol][dataset] = (fetched, skipped)
                all_failures.extend(failures)

            line = "  ".join(
                f"{dataset} {report[symbol][dataset][0]}+{report[symbol][dataset][1]}"
                for dataset in datasets
            )
            print(f"{symbol:<18} {line}", flush=True)

    print("\nСводка «символ → дней выгружено» (скачано сейчас + уже лежало):")
    for symbol in symbols:
        total_days = {
            dataset: sum(report[symbol][dataset]) for dataset in datasets
        }
        print(
            f"  {symbol:<18} "
            + "  ".join(f"{name} {value}" for name, value in total_days.items())
        )

    if all_failures:
        print(f"\nНе скачалось ({len(all_failures)}):")
        for line in all_failures[:50]:
            print(f"  {line}")

    return 1 if all_failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
