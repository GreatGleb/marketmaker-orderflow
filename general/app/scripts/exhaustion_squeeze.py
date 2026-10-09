"""Проверка ТЗ трейдера «Post-Impulse Short Squeeze Reversal» по aggTrades.

Запускается руками, в `supervisord.conf` не добавляется.

Трейдер прислал постановку боевого бота 2026-10-03: шорт после роста
≥2% за ≤2 с, выход через 30 с, кулдаун 180 с, не больше двух позиций.
Это ячейка «2 с / ≥2% / 30 с, шорт» из `squeeze_express.py`, которая в
сентябре дала нетто −0.24% (раздел 14.3 плана 15). Новое в ТЗ одно —
**фильтр затухания**: входить, только когда объём агрессивных покупок за
последние 100 мс упал минимум на 70% от пика сквиза. Скрипт проверяет,
меняет ли этот фильтр знак.

Правило зафиксировано до счёта (раздел 16 плана 15):

* импульс — `last[t] / last[t - 2 с] - 1 >= 2%` на сетке 100 мс, как в
  `squeeze_express`;
* пик — максимум покупательского объёма в USDT по тактам 100 мс внутри
  окна импульса `(t - 2 с; t]`;
* вход — первый такт начиная с `t`, где покупки не больше
  `EXHAUSTION_SHARE` пика; ждём не дольше `MAX_WAIT_SECONDS`, иначе
  сигнал пропущен. Цена — последняя сделка через `ENTRY_LAG_MS` после
  этого такта (сеть и биржа);
* выход — последняя сделка через 30 с после входа;
* события по символу не ближе 180 с.

Лимит двух позиций и вселенная «топ-100 по объёму > $20M» применяются в
`summary` поверх событий: для них нужны все пары сразу. Объём берётся за
предыдущий день, чтобы не заглядывать вперёд.

Запуск (внутри контейнера):

    docker exec -it orderflow_general python -m app.scripts.exhaustion_squeeze --month 2026-08
    docker exec -it orderflow_general python -m app.scripts.exhaustion_squeeze --month 2026-09
"""
import argparse
import os
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
)
from app.scripts.squeeze_express import (
    COST_MARKET,
    all_symbols,
    cluster_bootstrap,
    day_t_stat,
)
from app.scripts.volume_burst_precision import day_arrays

WINDOW_SECONDS = 2
MOVE_PERCENT = 2.0
EXHAUSTION_SHARE = 0.30
MAX_WAIT_SECONDS = 5
ENTRY_LAG_MS = 200
HOLD_SECONDS = 30
COOLDOWN_SECONDS = 180
MAX_OPEN_POSITIONS = 2
TOP_PAIRS = 100
MIN_DAY_VOLUME_USDT = 20_000_000

SPAN = WINDOW_SECONDS * BUCKETS_PER_SECOND
WAIT = MAX_WAIT_SECONDS * BUCKETS_PER_SECOND
LAG = ENTRY_LAG_MS // GRID_MS
HOLD = HOLD_SECONDS * BUCKETS_PER_SECOND
COOLDOWN = COOLDOWN_SECONDS * BUCKETS_PER_SECOND


def process_day(task):
    """События дня: сквизы вверх и исход шорта с фильтром и без."""
    symbol, path = task
    arrays = day_arrays(path)

    if arrays is None:
        return [], None

    last, volume, signed, _ = arrays
    day = path.name[-14:-4]
    buys = (volume + signed) / 2
    day_volume = float(volume.sum())

    with np.errstate(invalid="ignore"):
        change = np.full(BUCKETS_PER_DAY, np.nan)
        change[SPAN:] = (last[SPAN:] / last[:-SPAN] - 1) * 100

    fired = np.nan_to_num(change) >= MOVE_PERCENT
    fired[BUCKETS_PER_DAY - (WAIT + LAG + HOLD + 1):] = False
    rows = []
    next_allowed = -1

    for trigger in np.flatnonzero(fired):
        if trigger < next_allowed:
            continue

        peak = buys[trigger - SPAN + 1:trigger + 1].max()
        calm = np.flatnonzero(
            buys[trigger:trigger + WAIT + 1] <= EXHAUSTION_SHARE * peak
        )
        row = {
            "symbol": symbol,
            "day": day,
            "bucket": int(trigger),
            "impulse": float(change[trigger]),
            "peak_buys": float(peak),
            "base_gross": short_gross(last, trigger + LAG),
        }

        if calm.size:
            signal = trigger + int(calm[0])
            row["wait_ms"] = int(calm[0]) * GRID_MS
            row["entry_bucket"] = signal + LAG
            row["gross"] = short_gross(last, signal + LAG)
            next_allowed = signal + LAG + HOLD + COOLDOWN
        else:
            row["wait_ms"] = np.nan
            row["entry_bucket"] = np.nan
            row["gross"] = np.nan
            next_allowed = trigger + COOLDOWN

        rows.append(row)

    return rows, (symbol, day, day_volume)


def short_gross(last: np.ndarray, entry_at: int) -> float:
    return (1 - last[entry_at + HOLD] / last[entry_at]) * 100


def top_universe(volumes: pd.DataFrame) -> set[tuple[str, str]]:
    """(символ, день), где вчерашний объём в топ-100 и выше $20M."""
    table = volumes.pivot(index="day", columns="symbol", values="volume")
    previous = table.sort_index().shift(1)
    allowed = set()

    for day, row in previous.iterrows():
        row = row.dropna()
        row = row[row > MIN_DAY_VOLUME_USDT].nlargest(TOP_PAIRS)
        allowed.update((symbol, day) for symbol in row.index)

    return allowed


def position_limit(events: pd.DataFrame) -> pd.Series:
    """Маска сделок, которые прошли бы при лимите открытых позиций."""
    start = pd.to_datetime(events["day"]).astype("int64") // 10**6 \
        + events["entry_bucket"].astype("int64") * GRID_MS
    order = np.argsort(start.to_numpy(), kind="stable")
    ends = []
    keep = np.zeros(len(events), dtype=bool)

    for position in order:
        moment = start.iloc[position]
        ends = [end for end in ends if end > moment]

        if len(ends) < MAX_OPEN_POSITIONS:
            keep[position] = True
            ends.append(moment + HOLD_SECONDS * 1000)

    return pd.Series(keep, index=events.index)


def describe(name: str, gross: pd.Series, days: pd.Series,
             clusters: np.ndarray, rng) -> dict:
    net = gross - COST_MARKET
    low, high = cluster_bootstrap(net.to_numpy(), clusters, rng)
    t_days, total_days, days_up = day_t_stat(net, days)
    first_half = days.str[-2:].astype(int) <= 15
    return {
        "срез": name,
        "n": len(gross),
        "валовое": gross.mean(),
        "нетто 0.15": net.mean(),
        "ДИ 0.15": f"[{low:+.3f}; {high:+.3f}]",
        "t дни": t_days,
        "дней +/всего": f"{days_up}/{total_days}",
        "винрейт": (net > 0).mean(),
        "без топ-10": net.sort_values().iloc[:-10].mean()
        if len(net) > 20 else np.nan,
        "валовое 1–15": gross[first_half].mean(),
        "валовое 16–": gross[~first_half].mean(),
    }


def summary(events: pd.DataFrame, volumes: pd.DataFrame) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    allowed = top_universe(volumes)
    in_top = pd.Series(
        [(s, d) in allowed for s, d in zip(events["symbol"], events["day"])],
        index=events.index,
    )
    passed = events["gross"].notna()
    slices = [
        ("вся вселенная, без фильтра", events, "base_gross"),
        ("вся вселенная, с фильтром", events[passed], "gross"),
        ("топ-100, без фильтра", events[in_top], "base_gross"),
        ("топ-100, с фильтром", events[in_top & passed], "gross"),
    ]
    top_passed = events[in_top & passed]
    slices.append((
        "топ-100, фильтр, ≤2 позиций",
        top_passed[position_limit(top_passed)], "gross",
    ))
    rows = []

    for name, group, column in slices:
        clusters = (group["symbol"] + "|" + group["day"]).to_numpy()
        rows.append(describe(name, group[column], group["day"], clusters, rng))

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--month", default="2026-09")
    parser.add_argument("--symbols", help="через запятую; по умолчанию все")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR))
    symbols = (
        args.symbols.split(",") if args.symbols
        else all_symbols(root, args.month)
    )
    tasks = []

    for symbol in symbols:
        folder = root / AGG_TRADES_DIR.format(symbol=symbol)
        tasks.extend(
            (symbol, path) for path in
            sorted(folder.glob(f"{symbol}-aggTrades-{args.month}-*.zip"))
        )

    print(f"{args.month}: пар {len(symbols)}, файлов {len(tasks)}", flush=True)
    rows, volumes = [], []

    with ProcessPoolExecutor(args.workers) as pool:
        for done, (day_rows, day_volume) in enumerate(
            pool.map(process_day, tasks, chunksize=4), 1,
        ):
            rows.extend(day_rows)

            if day_volume:
                volumes.append(day_volume)

            if done % 1000 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    events = pd.DataFrame(rows)
    volumes = pd.DataFrame(volumes, columns=["symbol", "day", "volume"])
    out = root / "analysis" / f"exhaustion_squeeze_{args.month}"
    events.to_csv(f"{out}_events.csv.gz", index=False)

    table = summary(events, volumes)
    table.to_csv(f"{out}.csv", index=False)

    passed = events["wait_ms"].notna()
    print(f"\nсквизов {len(events)}, фильтр пройден {passed.mean():.1%}, "
          f"ожидание мс: медиана {events['wait_ms'].median():.0f}, "
          f"доля с 0 мс {(events['wait_ms'] == 0).mean():.1%}")

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:+.3f}")
    print(f"\nШорт сквиза ≥{MOVE_PERCENT}% за ≤{WINDOW_SECONDS} с, "
          f"выход через {HOLD_SECONDS} с, %:")
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
