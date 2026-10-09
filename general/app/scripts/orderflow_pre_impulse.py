"""Опережающие признаки: OBI (L5/L10) и CVD за 500 мс до начала импульса.

Запускается руками, в `supervisord.conf` не добавляется.

Вопрос, на который отвечает скрипт: **видно ли в книге и в потоке
сделок что-нибудь до того, как цена пошла**. Не во время движения —
урок раздела 9.4 плана (`.ai/docs/test-bots/15-orderflow-plan.md`):
дельта в окне, которое движение создало, совпадает с ним по знаку в 97%
случаев и признаком не является. Поэтому окно признаков здесь кончается
там, где импульс самое раннее мог начаться, и в него не попадает ни одна
сделка самого импульса.

**Где T0.** Импульс — изменение последней цены на сетке 100 мс не меньше
`--move` % за `--window` с (`first_crossings` из `impulse_express`, как
в `squeeze_express`). Срабатывание в такте `trigger` сравнивает цену в
конце такта `trigger − span` с ценой в конце `trigger`, значит движение
целиком лежит правее конца такта `trigger − span`. Это и есть T0: самое
раннее возможное начало. Настоящее начало может быть позже — тогда окно
признаков захватит спокойные сотни миллисекунд перед ним, но не само
движение. Ошибка в эту сторону разбавляет эффект, в обратную —
подделывает его.

**Контроль.** Признак «OBI перед выбросом вверх положителен» ничего не
значит без частоты того же OBI в обычные моменты. На каждый день пары
берутся `--controls` случайных моментов не ближе `CONTROL_EXCLUSION_SEC`
к любому событию, и признаки считаются тем же кодом. Опережение — это
разница распределений событий и контроля (AUC), а не средний OBI перед
событием.

**Признаки в окне [T0 − 500 мс, T0):**

* `obi_l5`, `obi_l10` — `(bid − ask) / (bid + ask)` по объёму первых 5 и
  10 уровней снимка книги, последнего с `E < T0`
  (`order_book_listener.order_book_imbalance`, та же формула);
* `obi_l5_delta`, `obi_l10_delta` — то же минус OBI снимка на начало
  окна. Ноль, если снимок один и тот же — см. `book_same` ниже;
* `book_age_ms` — возраст снимка на T0. **Архив пишет снимок раз в
  секунду** (`ORDER_BOOK_ARCHIVE_SEC`), так что при окне 500 мс снимок
  чаще всего старше окна; старше `MAX_BOOK_AGE_MS` — признак пустой;
* `book_same` — снимок на начало и на конец окна один и тот же, дельта
  OBI неинформативна;
* `spread_bps` — спред снимка;
* `cvd_quote` — объём покупок агрессором минус продаж, в USDT;
  `is_buyer_maker = true` — агрессор продавец;
* `cvd_share` — то же, деленное на весь объём окна, от −1 до +1;
* `volume_burst` — объём окна против среднего 500-мс объёма за
  предыдущие `BASELINE_SEC` секунд;
* `trades` — число агрегированных сделок в окне.

**Исход** для событий — ход цены от входа через `ENTRY_LAG_MS` до
`HOLDS_SECONDS`, в процентах, со знаком импульса (плюс — продолжение).
Для шорта выброса вверх это минус валовое `squeeze_express`.

**Какие дни годятся.** Нужны обе половины: `aggTrades` дня (бакет
data.binance.vision выкладывает их на следующие сутки) и архив стакана
за те же сутки. Скрипт берёт только пересечение и печатает, сколько
дней пар в него попало. На 2026-09-27 это 24 и 25 сентября (25-го
стакан до 16:49 UTC) — события считаются сотнями, а не тысячами, и
выводы по порогам делать рано: это заготовка, которая растёт вместе с
архивом.

Запуск (внутри контейнера):

    docker exec -it orderflow_general python -m app.scripts.orderflow_pre_impulse --days 2026-09-24,2026-09-25
"""
import argparse
import gzip
import json
import math
import os
import zipfile
import zlib
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from app.scripts.impulse_express import (
    AGG_TRADES_DIR,
    BUCKETS_PER_DAY,
    BUCKETS_PER_SECOND,
    DEFAULT_HISTORY_DIR,
    GRID_MS,
    day_grid,
    first_crossings,
)

ORDER_BOOK_DIR = "order_book/{symbol}"
# Tardis `book_snapshot_25` (`fetch_tardis_snapshots.py`): снимок на каждое
# изменение книги, 25 уровней, метки в микросекундах.
TARDIS_BOOK_PATH = (
    "tardis/binance-futures/book_snapshot_25/{y}/{m}/{d}/{symbol}.csv.gz"
)
TARDIS_VERIFY_FILE = "tardis/verify_book_snapshot_25.csv"
PRE_WINDOW_MS = 500
OBI_LEVELS = (5, 10)
# Снимок старше этого на T0 — книги на момент признака у нас нет. Секунда
# плюс запас: архив пишет раз в секунду, а обрывы сокетов дают дыры.
MAX_BOOK_AGE_MS = 1500
# У Tardis долгая пауза между снимками значит «книга не менялась», и
# старый снимок остаётся верным. Предел — только от дыр в самой записи.
TARDIS_MAX_BOOK_AGE_MS = 600_000
BOOK_AGE_LIMITS = {"own": MAX_BOOK_AGE_MS, "tardis": TARDIS_MAX_BOOK_AGE_MS}
BASELINE_SEC = 300

ENTRY_LAG_MS = 200
HOLDS_SECONDS = (30, 180)
EVENT_COOLDOWN_SECONDS = 180

CONTROLS_PER_DAY = 200
CONTROLS_PER_EVENT = 5
CONTROL_EXCLUSION_SEC = 60
# «Спокойный» контрольный момент: цена не сдвинулась на столько за окно
# импульса ни до момента, ни сразу после. Без фильтра в контроль попадают
# мелкие импульсы, и разница с событиями размывается.
CALM_MOVE_PERCENT = 0.5
# День пары, где книга началась позже этого, — день листинга: другое
# явление, и базовой линии перед ним нет.
LISTING_START_SEC = 600

DAY_MS = BUCKETS_PER_DAY * GRID_MS


# --- Загрузка ---------------------------------------------------------------

def load_trades(path: Path) -> pd.DataFrame | None:
    """Сделки дня: время, цена, объём в USDT и знак агрессора."""
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as handle:
            frame = pd.read_csv(
                handle,
                usecols=["price", "quantity", "transact_time", "is_buyer_maker"],
                dtype={"price": np.float64, "quantity": np.float64,
                       "transact_time": np.int64},
            )

    if frame.empty:
        return None

    maker = frame["is_buyer_maker"].astype(str).str.lower() == "true"
    return pd.DataFrame({
        "time": frame["transact_time"].to_numpy(),
        "quote": (frame["price"] * frame["quantity"]).to_numpy(),
        # Покупатель — мейкер, значит, агрессор продавал.
        "sign": np.where(maker, -1.0, 1.0),
    })


def load_book(path: Path) -> pd.DataFrame | None:
    """Снимки книги дня: время `E`, OBI на каждой глубине, спред."""
    rows = []

    with gzip.open(path, "rt") as handle:
        for line in handle:
            try:
                frame = json.loads(line)
                bids = np.array(frame["b"], dtype=np.float64)
                asks = np.array(frame["a"], dtype=np.float64)
            except (ValueError, KeyError, TypeError):
                continue

            if not len(bids) or not len(asks):
                continue

            row = {"time": int(frame["E"])}

            for levels in OBI_LEVELS:
                bid = bids[:levels, 1].sum()
                ask = asks[:levels, 1].sum()
                total = bid + ask
                row[f"obi_l{levels}"] = (bid - ask) / total if total > 0 else np.nan

            mid = (bids[0, 0] + asks[0, 0]) / 2
            row["spread_bps"] = (asks[0, 0] - bids[0, 0]) / mid * 1e4
            rows.append(row)

    if not rows:
        return None

    return pd.DataFrame(rows).sort_values("time", kind="stable").reset_index(
        drop=True
    )


def load_book_tardis(path: Path) -> pd.DataFrame | None:
    """Снимки Tardis `book_snapshot_25` в том же виде, что `load_book`.

    Время приводится к миллисекундам отбрасыванием дробной части: снимок
    в T0 − 0.4 мс остаётся строго раньше T0, снимок в T0 + 0.3 мс — нет.
    """
    depth = max(OBI_LEVELS)
    columns = ["timestamp", "asks[0].price", "bids[0].price"]
    columns += [f"{side}[{i}].amount" for side in ("asks", "bids")
                for i in range(depth)]
    frame = pd.read_csv(path, usecols=columns)

    if frame.empty:
        return None

    out = {"time": (frame["timestamp"].to_numpy() // 1000).astype(np.int64)}

    for levels in OBI_LEVELS:
        bid = frame[[f"bids[{i}].amount" for i in range(levels)]].sum(axis=1)
        ask = frame[[f"asks[{i}].amount" for i in range(levels)]].sum(axis=1)
        total = (bid + ask).to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            out[f"obi_l{levels}"] = np.where(
                total > 0, (bid - ask).to_numpy() / total, np.nan
            )

    best_ask = frame["asks[0].price"].to_numpy()
    best_bid = frame["bids[0].price"].to_numpy()
    out["spread_bps"] = (best_ask - best_bid) / ((best_ask + best_bid) / 2) * 1e4

    return pd.DataFrame(out).sort_values("time", kind="stable").reset_index(
        drop=True
    )


BOOK_LOADERS = {"own": load_book, "tardis": load_book_tardis}


# --- Признаки ---------------------------------------------------------------

def impulse_start_ms(day_start: int, trigger: int, window_seconds: float) -> int:
    """T0: конец такта `trigger − span`, самое раннее начало движения."""
    span = int(window_seconds * BUCKETS_PER_SECOND)
    return day_start + (trigger - span + 1) * GRID_MS


def book_features(
    book: pd.DataFrame | None, t0: int, max_age_ms: int = MAX_BOOK_AGE_MS,
) -> dict:
    """OBI снимка на T0 и его изменение за окно. Только `E < t0`."""
    empty = {f"obi_l{n}": np.nan for n in OBI_LEVELS}
    empty.update({f"obi_l{n}_delta": np.nan for n in OBI_LEVELS})
    empty.update(book_age_ms=np.nan, book_same=np.nan, spread_bps=np.nan)

    if book is None:
        return empty

    times = book["time"].to_numpy()
    # Строго раньше T0: снимок с E == T0 уже мог видеть начало движения.
    end = np.searchsorted(times, t0, side="left") - 1

    if end < 0 or t0 - times[end] > max_age_ms:
        return empty

    start = np.searchsorted(times, t0 - PRE_WINDOW_MS, side="left") - 1
    out = {"book_age_ms": float(t0 - times[end]),
           "spread_bps": book["spread_bps"].iat[end],
           "book_same": float(start == end)}

    for n in OBI_LEVELS:
        now = book[f"obi_l{n}"].iat[end]
        out[f"obi_l{n}"] = now
        out[f"obi_l{n}_delta"] = (
            now - book[f"obi_l{n}"].iat[start] if start >= 0 else np.nan
        )

    return out


def trade_features(trades: pd.DataFrame, cum: dict, t0: int) -> dict:
    """CVD и всплеск объёма в [t0 − 500 мс, t0). `cum` — из `trade_sums`."""
    times = trades["time"].to_numpy()
    lo = np.searchsorted(times, t0 - PRE_WINDOW_MS, side="left")
    hi = np.searchsorted(times, t0, side="left")
    base_lo = np.searchsorted(times, t0 - BASELINE_SEC * 1000, side="left")

    def window_sum(key, a, b):
        return cum[key][b] - cum[key][a]

    volume = window_sum("quote", lo, hi)
    cvd = window_sum("signed", lo, hi)
    baseline = window_sum("quote", base_lo, lo)
    baseline_windows = (BASELINE_SEC * 1000 - PRE_WINDOW_MS) / PRE_WINDOW_MS

    return {
        "cvd_quote": cvd,
        "cvd_share": cvd / volume if volume > 0 else np.nan,
        "volume_burst": (
            volume / (baseline / baseline_windows) if baseline > 0 else np.nan
        ),
        "trades": int(hi - lo),
    }


def trade_sums(trades: pd.DataFrame) -> dict:
    """Накопленные суммы с нулём в начале: сумма окна — два обращения."""
    quote = trades["quote"].to_numpy()
    return {
        "quote": np.concatenate(([0.0], np.cumsum(quote))),
        "signed": np.concatenate(([0.0], np.cumsum(quote * trades["sign"]))),
    }


def control_buckets(
    events: np.ndarray, reach: int, count: int, rng,
    calm: np.ndarray | None = None,
) -> np.ndarray:
    """Случайные такты дня не ближе `CONTROL_EXCLUSION_SEC` к событиям.

    `calm` — маска допустимых тактов (см. `calm_mask`); без неё годится
    любой такт вне окрестности событий.
    """
    allowed = np.ones(BUCKETS_PER_DAY, dtype=bool)
    allowed[:BASELINE_SEC * BUCKETS_PER_SECOND] = False
    allowed[BUCKETS_PER_DAY - reach:] = False
    radius = CONTROL_EXCLUSION_SEC * BUCKETS_PER_SECOND

    for bucket in events:
        allowed[max(bucket - radius, 0):bucket + radius] = False

    if calm is not None:
        allowed &= calm

    pool = np.flatnonzero(allowed)

    if pool.size == 0:
        return pool

    return np.sort(rng.choice(pool, size=min(count, pool.size), replace=False))


def calm_mask(change: np.ndarray, span: int, limit: float) -> np.ndarray:
    """Такты, где цена не прошла `limit` % ни за окно до, ни за окно после.

    `change[b]` — ход за окно, кончающееся в `b`; окно после — это
    `change[b + span]`.
    """
    moved = np.abs(np.nan_to_num(change, nan=np.inf)) >= limit
    after = np.ones_like(moved)
    after[:-span] = moved[span:]
    return ~(moved | after)


# --- День пары --------------------------------------------------------------

def process_day(task: dict):
    """События всех порогов и контроль одного дня пары.

    `task`: symbol, day, trades_path, book_path, window, moves, source,
    control_mode (`per_day` — `controls` моментов на день, как было;
    `per_event` — `controls` на событие младшего порога, только в
    спокойные моменты), controls, gap_ms.
    """
    symbol, day = task["symbol"], task["day"]
    window, moves = task["window"], sorted(task["moves"])
    grid = day_grid(task["trades_path"])
    trades = load_trades(task["trades_path"])

    if grid is None or trades is None:
        return []

    last = grid[0]
    source = task.get("source", "own")
    max_age = BOOK_AGE_LIMITS[source]
    gap = task.get("gap_ms", 0)
    cum = trade_sums(trades)
    day_start = int(pd.Timestamp(day, tz="UTC").value // 1_000_000)
    span = int(window * BUCKETS_PER_SECOND)
    lag = ENTRY_LAG_MS // GRID_MS
    reach = lag + max(HOLDS_SECONDS) * BUCKETS_PER_SECOND + 1

    with np.errstate(invalid="ignore"):
        change = np.full(BUCKETS_PER_DAY, np.nan)
        change[span:] = (last[span:] / last[:-span] - 1) * 100

    samples = []
    all_events = []

    for move in moves:
        fired = np.abs(np.nan_to_num(change)) >= move
        fired[:BASELINE_SEC * BUCKETS_PER_SECOND] = False
        fired[BUCKETS_PER_DAY - reach:] = False
        events = first_crossings(
            fired, EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND
        )
        all_events.append(events)
        samples += [(int(b), "event", move) for b in events]

    # crc32, а не hash(): тот солится заново в каждом процессе, и контроль
    # не воспроизводился бы от запуска к запуску.
    rng = np.random.default_rng(zlib.crc32(f"{symbol}|{day}".encode()))
    events_any = np.unique(np.concatenate(all_events)) if all_events else []

    if task.get("control_mode", "per_day") == "per_event":
        # Контроль привязан к событиям: без них день пары ничего не даёт,
        # а книга Tardis — самое дорогое в счёте, её не читаем зря.
        if not samples:
            return []

        count = task["controls"] * len(all_events[0])
        limit = task.get("calm_move", CALM_MOVE_PERCENT)
        # Ноль или меньше — без фильтра: честные случайные моменты дня.
        calm = calm_mask(change, span, limit) if limit > 0 else None
    else:
        count, calm = task["controls"], None

    samples += [(int(b), "control", np.nan) for b in control_buckets(
        events_any, reach, count, rng, calm)]
    book = BOOK_LOADERS[source](task["book_path"])
    rows = []

    for bucket, kind, move in samples:
        if kind == "event":
            direction = 1 if change[bucket] > 0 else -1
            t0 = impulse_start_ms(day_start, bucket, window)
        else:
            direction = 0
            # У контроля «начало импульса» — сам такт: окно признаков
            # кончается там же, где у события.
            t0 = day_start + (bucket + 1) * GRID_MS

        # `gap_ms` сдвигает окно признаков раньше T0: так проверяется, не
        # несёт ли признак начало самого движения.
        at = t0 - gap
        row = {"symbol": symbol, "day": day, "kind": kind,
               "move_threshold": move, "bucket": bucket, "t0_ms": t0,
               "direction": direction,
               "impulse": abs(change[bucket]) if kind == "event" else np.nan}
        row.update(book_features(book, at, max_age))
        row.update(trade_features(trades, cum, at))

        entry_at = bucket + lag
        entry = last[entry_at]

        for hold in HOLDS_SECONDS:
            exit_at = entry_at + hold * BUCKETS_PER_SECOND
            move_after = (last[exit_at] / entry - 1) * 100
            # Для событий — по импульсу (плюс — продолжение), для
            # контроля — просто ход цены.
            row[f"after_{hold}s"] = move_after * (direction or 1)

        rows.append(row)

    return rows


def available_days(
    root: Path, days: list[str], source: str = "own",
    pairs: set[tuple[str, str]] | None = None, skip_listing: bool = False,
) -> list[tuple]:
    """Дни пар, у которых есть и `aggTrades`, и архив стакана.

    `pairs` — только эти (символ, день); `skip_listing` — выбросить дни
    листинга по `TARDIS_VERIFY_FILE` (книга началась позже
    `LISTING_START_SEC`).
    """
    tasks = []

    if source == "tardis":
        listing = set()
        verify = root / TARDIS_VERIFY_FILE

        if skip_listing and verify.exists():
            table = pd.read_csv(verify)
            late = table[table["starts_after_s"] > LISTING_START_SEC]
            listing = set(zip(late["symbol"], late["day"]))

        for day in days:
            y, m, d = day.split("-")
            folder = root / TARDIS_BOOK_PATH.format(y=y, m=m, d=d, symbol="x"
                                                     ).rsplit("/", 1)[0]

            if not folder.exists():
                continue

            for book in sorted(folder.glob("*.csv.gz")):
                symbol = book.name[:-len(".csv.gz")]

                if pairs is not None and (symbol, day) not in pairs:
                    continue

                if (symbol, day) in listing:
                    continue

                trades = (root / AGG_TRADES_DIR.format(symbol=symbol)
                          / f"{symbol}-aggTrades-{day}.zip")

                if trades.exists():
                    tasks.append((symbol, day, trades, book))

        return tasks

    book_root = root / ORDER_BOOK_DIR.format(symbol="")

    if not book_root.exists():
        return tasks

    for folder in sorted(book_root.iterdir()):
        symbol = folder.name

        for day in days:
            if pairs is not None and (symbol, day) not in pairs:
                continue

            book = folder / f"{symbol}-depth20-{day}.jsonl.gz"
            trades = (root / AGG_TRADES_DIR.format(symbol=symbol)
                      / f"{symbol}-aggTrades-{day}.zip")

            if book.exists() and trades.exists():
                tasks.append((symbol, day, trades, book))

    return tasks


# --- Сводка -----------------------------------------------------------------

def auc(positive: np.ndarray, negative: np.ndarray) -> float:
    """P(признак события > признака контроля), ранговая форма Манна-Уитни.

    0.5 — признак не отличает событие от обычного момента. Считается по
    направлению импульса отдельно: у выброса вверх опережающий OBI
    ожидается положительным, у провала — отрицательным, и в общей куче
    они друг друга гасят.
    """
    positive = positive[~np.isnan(positive)]
    negative = negative[~np.isnan(negative)]

    if not positive.size or not negative.size:
        return np.nan

    ranks = pd.Series(np.concatenate((positive, negative))).rank().to_numpy()
    rank_sum = ranks[:positive.size].sum()
    return (rank_sum - positive.size * (positive.size + 1) / 2) / (
        positive.size * negative.size
    )


FEATURES = ("obi_l5", "obi_l10", "obi_l5_delta", "obi_l10_delta",
            "cvd_share", "volume_burst", "spread_bps")


def summary(frame: pd.DataFrame) -> pd.DataFrame:
    control = frame[frame["kind"] == "control"]
    rows = []

    for direction, name in ((1, "вверх"), (-1, "вниз")):
        events = frame[(frame["kind"] == "event")
                       & (frame["direction"] == direction)]

        for feature in FEATURES:
            ev = events[feature].to_numpy(dtype=float)
            ct = control[feature].to_numpy(dtype=float)
            rows.append({
                "импульс": name,
                "признак": feature,
                "событий": int(np.sum(~np.isnan(ev))),
                "контроль": int(np.sum(~np.isnan(ct))),
                "медиана событий": np.nanmedian(ev) if ev.size else np.nan,
                "медиана контроля": np.nanmedian(ct) if ct.size else np.nan,
                "AUC": auc(ev, ct),
            })

    return pd.DataFrame(rows)


def _betacf(a: float, b: float, x: float) -> float:
    """Цепная дробь неполной бета-функции (метод Лентца)."""
    tiny = 1e-300
    c, d = 1.0, 1.0 - (a + b) * x / (a + 1)
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d

    for m in range(1, 300):
        m2 = 2 * m
        for numerator in (
            m * (b - m) * x / ((a + m2 - 1) * (a + m2)),
            -(a + m) * (a + b + m) * x / ((a + m2) * (a + m2 + 1)),
        ):
            d = 1 + numerator * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + numerator / c
            c = c if abs(c) > tiny else tiny
            h *= d * c

        if abs(d * c - 1) < 1e-14:
            break

    return h


def betainc(a: float, b: float, x: float) -> float:
    """Регуляризованная неполная бета-функция I_x(a, b)."""
    if x <= 0:
        return 0.0

    if x >= 1:
        return 1.0

    front = np.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log(1 - x)
    )

    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a

    return 1 - front * _betacf(b, a, 1 - x) / b


def t_two_sided_p(t: float, df: int) -> float:
    """Двусторонний p-value t-распределения; scipy в образе нет."""
    if not np.isfinite(t) or df <= 0:
        return np.nan

    return betainc(df / 2, 0.5, df / (df + t * t))


def cluster_diff(values: np.ndarray, is_event: np.ndarray,
                 clusters: np.ndarray) -> dict:
    """Разность средних событие − контроль с ошибкой, кластеризованной по дню.

    МНК `y = a + b·D` с сэндвичем CR1: наблюдения одного дня
    коррелированы (общий режим рынка, каскады сквизов), и ошибка по
    отдельным наблюдениям была бы в разы уже, чем данные позволяют.
    Степени свободы — число кластеров минус один, t-распределение.
    """
    keep = ~np.isnan(values)
    y, d, g = values[keep], is_event[keep].astype(float), clusters[keep]
    codes, inverse = np.unique(g, return_inverse=True)
    n, k, groups = len(y), 2, len(codes)

    if groups < 2 or d.sum() == 0 or d.sum() == n:
        return {"разность": np.nan, "SE кл.": np.nan, "t": np.nan,
                "p": np.nan, "кластеров": groups}

    x = np.column_stack((np.ones(n), d))
    xtx_inv = np.linalg.inv(x.T @ x)
    beta = xtx_inv @ x.T @ y
    residual = y - x @ beta
    scores = np.zeros((groups, k))
    np.add.at(scores, inverse, x * residual[:, None])
    meat = scores.T @ scores
    correction = groups / (groups - 1) * (n - 1) / (n - k)
    variance = correction * xtx_inv @ meat @ xtx_inv
    se = float(np.sqrt(variance[1, 1]))
    t = float(beta[1] / se) if se > 0 else np.nan

    return {"разность": float(beta[1]), "SE кл.": se, "t": t,
            "p": t_two_sided_p(t, groups - 1), "кластеров": groups}


STAT_FEATURES = ("obi_l5", "obi_l10", "obi_l5_delta", "obi_l10_delta",
                 "cvd_share", "cvd_quote", "volume_burst")


def cluster_summary(frame: pd.DataFrame) -> pd.DataFrame:
    """Импульсы против контроля: описательные и t по дням, по порогу и знаку."""
    control = frame[frame["kind"] == "control"]
    rows = []
    thresholds = sorted(frame.loc[frame["kind"] == "event",
                                  "move_threshold"].dropna().unique())

    for move in thresholds:
        for direction, name in ((1, "вверх"), (-1, "вниз")):
            events = frame[(frame["kind"] == "event")
                           & (frame["move_threshold"] == move)
                           & (frame["direction"] == direction)]
            # Контроль — из тех же дней пар, где были события этой группы.
            keys = set(zip(events["symbol"], events["day"]))
            matched = control[[k in keys for k in
                               zip(control["symbol"], control["day"])]]
            both = pd.concat([events, matched])
            is_event = (both["kind"] == "event").to_numpy()
            days = both["day"].to_numpy()

            for feature in STAT_FEATURES:
                values = both[feature].to_numpy(dtype=float)
                ev = values[is_event]
                ct = values[~is_event]
                ev, ct = ev[~np.isnan(ev)], ct[~np.isnan(ct)]
                row = {
                    "порог": f">={move:g}%", "импульс": name,
                    "признак": feature,
                    "n соб.": ev.size,
                    "n контр.": ct.size,
                    "среднее соб.": np.nanmean(ev) if ev.size else np.nan,
                    "среднее контр.": np.nanmean(ct) if ct.size else np.nan,
                    "медиана соб.": np.nanmedian(ev) if ev.size else np.nan,
                    "медиана контр.": np.nanmedian(ct) if ct.size else np.nan,
                    "σ соб.": np.nanstd(ev, ddof=1) if ev.size > 1 else np.nan,
                    "σ контр.": np.nanstd(ct, ddof=1) if ct.size > 1 else np.nan,
                }
                row.update(cluster_diff(values, is_event, days))
                row["AUC"] = auc(ev, ct)
                rows.append(row)

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", required=True,
                        help="через запятую, ГГГГ-ММ-ДД")
    parser.add_argument("--source", choices=sorted(BOOK_LOADERS),
                        default="own",
                        help="own — свой архив depth20, tardis — "
                             "book_snapshot_25 из fetch_tardis_snapshots")
    parser.add_argument("--window", type=float, default=2.0,
                        help="окно импульса, с")
    parser.add_argument("--move", type=float, default=2.0,
                        help="порог импульса, %%")
    parser.add_argument("--moves",
                        help="несколько порогов через запятую вместо --move")
    parser.add_argument("--control-mode", choices=("per_day", "per_event"),
                        default="per_day")
    parser.add_argument("--controls", type=int,
                        help=f"per_day: моментов на день (по умолчанию "
                             f"{CONTROLS_PER_DAY}); per_event: на событие "
                             f"({CONTROLS_PER_EVENT})")
    parser.add_argument("--calm-move", type=float, default=CALM_MOVE_PERCENT,
                        help="порог «спокойного» контроля, %%; 0 — без фильтра")
    parser.add_argument("--gap-ms", type=int, default=0,
                        help="сдвинуть окно признаков раньше T0")
    parser.add_argument("--pairs-file",
                        help="CSV с колонками day,symbol: только эти дни пар")
    parser.add_argument("--skip-listing", action="store_true",
                        help="выбросить дни листинга (только tardis)")
    parser.add_argument("--tag", default="", help="суффикс имени выгрузки")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR))
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    moves = ([float(m) for m in args.moves.split(",")] if args.moves
             else [args.move])
    controls = args.controls or (
        CONTROLS_PER_EVENT if args.control_mode == "per_event"
        else CONTROLS_PER_DAY
    )
    pairs = None

    if args.pairs_file:
        table = pd.read_csv(args.pairs_file)
        pairs = set(zip(table["symbol"], table["day"]))

    found = available_days(root, days, args.source, pairs, args.skip_listing)
    tasks = [{
        "symbol": symbol, "day": day, "trades_path": trades,
        "book_path": book, "window": args.window, "moves": moves,
        "source": args.source, "control_mode": args.control_mode,
        "controls": controls, "calm_move": args.calm_move,
        "gap_ms": args.gap_ms,
    } for symbol, day, trades, book in found]
    print(f"Дней пар со сделками и стаканом ({args.source}): {len(tasks)}",
          flush=True)

    rows = []

    with ProcessPoolExecutor(args.workers) as pool:
        for done, day_rows in enumerate(
            pool.map(process_day, tasks, chunksize=2), 1,
        ):
            rows.extend(day_rows)

            if done % 100 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    frame = pd.DataFrame(rows)

    if frame.empty:
        print("Событий нет.")
        return

    tag = f"{args.window:g}s_" + "_".join(f"{m:g}" for m in moves) + "pct"

    if args.source != "own":
        tag += f"_{args.source}"

    if args.gap_ms:
        tag += f"_gap{args.gap_ms}"

    tag += f"_{args.tag}" if args.tag else ""
    out = root / "analysis" / f"orderflow_pre_impulse_{tag}"
    frame.to_csv(f"{out}_samples.csv.gz", index=False)
    table = summary(frame)
    table.to_csv(f"{out}.csv", index=False)
    stats = cluster_summary(frame)
    stats.to_csv(f"{out}_clustered.csv", index=False)

    events = frame[frame["kind"] == "event"]
    print(f"Событий {len(events)} (вверх {int((events['direction'] > 0).sum())},"
          f" вниз {int((events['direction'] < 0).sum())}), контрольных "
          f"моментов {int((frame['kind'] == 'control').sum())}, дней "
          f"{frame['day'].nunique()}.")
    print(f"Книга не старше {BOOK_AGE_LIMITS[args.source]} мс на T0 у "
          f"{events['book_age_ms'].notna().mean():.0%} событий; медианный "
          f"возраст {events['book_age_ms'].median():.0f} мс; снимок не "
          f"сменился за окно у {events['book_same'].mean():.0%}.")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:+.4f}")
    print(f"\nПризнаки в [T0 − {args.gap_ms + PRE_WINDOW_MS} мс, "
          f"T0 − {args.gap_ms} мс), импульсы против контроля, t по дням:")
    print(stats.to_string(index=False))


if __name__ == "__main__":
    main()
