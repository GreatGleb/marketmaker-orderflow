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
PRE_WINDOW_MS = 500
OBI_LEVELS = (5, 10)
# Снимок старше этого на T0 — книги на момент признака у нас нет. Секунда
# плюс запас: архив пишет раз в секунду, а обрывы сокетов дают дыры.
MAX_BOOK_AGE_MS = 1500
BASELINE_SEC = 300

ENTRY_LAG_MS = 200
HOLDS_SECONDS = (30, 180)
EVENT_COOLDOWN_SECONDS = 180

CONTROLS_PER_DAY = 200
CONTROL_EXCLUSION_SEC = 60

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


# --- Признаки ---------------------------------------------------------------

def impulse_start_ms(day_start: int, trigger: int, window_seconds: float) -> int:
    """T0: конец такта `trigger − span`, самое раннее начало движения."""
    span = int(window_seconds * BUCKETS_PER_SECOND)
    return day_start + (trigger - span + 1) * GRID_MS


def book_features(book: pd.DataFrame | None, t0: int) -> dict:
    """OBI снимка на T0 и его изменение за окно. Только `E < t0`."""
    empty = {f"obi_l{n}": np.nan for n in OBI_LEVELS}
    empty.update({f"obi_l{n}_delta": np.nan for n in OBI_LEVELS})
    empty.update(book_age_ms=np.nan, book_same=np.nan, spread_bps=np.nan)

    if book is None:
        return empty

    times = book["time"].to_numpy()
    # Строго раньше T0: снимок с E == T0 уже мог видеть начало движения.
    end = np.searchsorted(times, t0, side="left") - 1

    if end < 0 or t0 - times[end] > MAX_BOOK_AGE_MS:
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


def control_buckets(events: np.ndarray, reach: int, count: int, rng) -> np.ndarray:
    """Случайные такты дня не ближе `CONTROL_EXCLUSION_SEC` к событиям."""
    allowed = np.ones(BUCKETS_PER_DAY, dtype=bool)
    allowed[:BASELINE_SEC * BUCKETS_PER_SECOND] = False
    allowed[BUCKETS_PER_DAY - reach:] = False
    radius = CONTROL_EXCLUSION_SEC * BUCKETS_PER_SECOND

    for bucket in events:
        allowed[max(bucket - radius, 0):bucket + radius] = False

    pool = np.flatnonzero(allowed)

    if pool.size == 0:
        return pool

    return np.sort(rng.choice(pool, size=min(count, pool.size), replace=False))


# --- День пары --------------------------------------------------------------

def process_day(task):
    symbol, day, trades_path, book_path, window, move, controls = task
    grid = day_grid(trades_path)
    trades = load_trades(trades_path)

    if grid is None or trades is None:
        return []

    last = grid[0]
    book = load_book(book_path)
    cum = trade_sums(trades)
    day_start = int(pd.Timestamp(day, tz="UTC").value // 1_000_000)
    span = int(window * BUCKETS_PER_SECOND)
    lag = ENTRY_LAG_MS // GRID_MS
    reach = lag + max(HOLDS_SECONDS) * BUCKETS_PER_SECOND + 1

    with np.errstate(invalid="ignore"):
        change = np.full(BUCKETS_PER_DAY, np.nan)
        change[span:] = (last[span:] / last[:-span] - 1) * 100

    fired = np.abs(np.nan_to_num(change)) >= move
    fired[:BASELINE_SEC * BUCKETS_PER_SECOND] = False
    fired[BUCKETS_PER_DAY - reach:] = False
    events = first_crossings(fired, EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND)

    # crc32, а не hash(): тот солится заново в каждом процессе, и контроль
    # не воспроизводился бы от запуска к запуску.
    rng = np.random.default_rng(zlib.crc32(f"{symbol}|{day}".encode()))
    rows = []

    samples = [(int(b), "event") for b in events]
    samples += [(int(b), "control")
                for b in control_buckets(events, reach, controls, rng)]

    for bucket, kind in samples:
        if kind == "event":
            direction = 1 if change[bucket] > 0 else -1
            t0 = impulse_start_ms(day_start, bucket, window)
        else:
            direction = 0
            # У контроля «начало импульса» — сам такт: окно признаков
            # кончается там же, где у события.
            t0 = day_start + (bucket + 1) * GRID_MS

        row = {"symbol": symbol, "day": day, "kind": kind,
               "bucket": bucket, "t0_ms": t0,
               "direction": direction,
               "impulse": abs(change[bucket]) if kind == "event" else np.nan}
        row.update(book_features(book, t0))
        row.update(trade_features(trades, cum, t0))

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


def available_days(root: Path, days: list[str]) -> list[tuple]:
    """Дни пар, у которых есть и `aggTrades`, и архив стакана."""
    book_root = root / ORDER_BOOK_DIR.format(symbol="")
    tasks = []

    if not book_root.exists():
        return tasks

    for folder in sorted(book_root.iterdir()):
        symbol = folder.name

        for day in days:
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", required=True,
                        help="через запятую, ГГГГ-ММ-ДД")
    parser.add_argument("--window", type=float, default=2.0,
                        help="окно импульса, с")
    parser.add_argument("--move", type=float, default=2.0,
                        help="порог импульса, %%")
    parser.add_argument("--controls", type=int, default=CONTROLS_PER_DAY)
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR))
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    found = available_days(root, days)
    tasks = [(*task, args.window, args.move, args.controls) for task in found]
    print(f"Дней пар со сделками и стаканом: {len(tasks)}", flush=True)

    rows = []

    with ProcessPoolExecutor(args.workers) as pool:
        for day_rows in pool.map(process_day, tasks, chunksize=2):
            rows.extend(day_rows)

    frame = pd.DataFrame(rows)

    if frame.empty:
        print("Событий нет.")
        return

    tag = f"{args.window:g}s_{args.move:g}pct"
    out = root / "analysis" / f"orderflow_pre_impulse_{tag}"
    frame.to_csv(f"{out}_samples.csv.gz", index=False)
    table = summary(frame)
    table.to_csv(f"{out}.csv", index=False)

    events = frame[frame["kind"] == "event"]
    print(f"Событий {len(events)} (вверх {int((events['direction'] > 0).sum())},"
          f" вниз {int((events['direction'] < 0).sum())}), контрольных "
          f"моментов {int((frame['kind'] == 'control').sum())}.")
    print(f"Книга свежее {MAX_BOOK_AGE_MS} мс на T0 у "
          f"{events['book_age_ms'].notna().mean():.0%} событий; медианный "
          f"возраст {events['book_age_ms'].median():.0f} мс; снимок не "
          f"сменился за окно у {events['book_same'].mean():.0%}.")

    pd.set_option("display.width", 200)
    pd.set_option("display.float_format", lambda v: f"{v:+.3f}")
    print(f"\nПризнаки в [T0 − {PRE_WINDOW_MS} мс, T0), события против "
          f"контроля:")
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
