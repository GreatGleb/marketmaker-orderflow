"""Точность всплеска объёма: сколько всплесков кончается сквизом и что дальше.

Запускается руками, в `supervisord.conf` не добавляется.

Раздел 15 плана (`.ai/docs/test-bots/15-orderflow-plan.md`): перед
сквизом объём за 500 мс заметно выше фона (AUC 0.63–0.67), и это не
артефакт контроля и не ранняя фаза движения (15.4). AUC говорит только,
что признак различает события и обычные моменты. Торговать можно
другое — долю сигналов, за которыми что-то происходит, и ход цены после
них. Это и считается здесь, по правилу из 15.5, записанному до счёта.

Нужны только `aggTrades`, поэтому счёт идёт по **всем** парам дня, а не
по дням пар, отобранным по сквизам: там точность была бы завышена самим
отбором.

**Сигнал** в такте `b` (сетка 100 мс): объём сделок в [b − 500 мс, b)
не меньше `K` × среднего 500-мс объёма за предыдущие `BASELINE_SEC`,
и сделок в окне не меньше `N`. Повтор на паре не раньше
`SIGNAL_COOLDOWN_SEC`. Направление — знак CVD окна.

**Исходы сигнала:**

* `sq15`, `sq20` — в пределах `SQUEEZE_HORIZON_MS` после сигнала
  сработал ход ≥1.5% (≥2%) за ≤2 с, как в `orderflow_pre_impulse`;
  `dsq15` — такой ход в сторону CVD;
* `r5`, `r30`, `r180` — ход последней цены от входа через
  `ENTRY_LAG_MS` в сторону CVD, %, валовой.

**База** — то же для всех тактов дня пары: доля моментов, после которых
в том же горизонте был сквиз, и средний модуль хода. **Полнота** — доля
сквизов (с паузой 180 с, как в счёте событий), которым предшествовал
сигнал в пределах того же горизонта.

Результат — суммы по (конфигурация, день), из них средние и t с
кластеризацией по дням (CR1 для среднего: остатки по дню). Подбор —
нечётные месяцы, проверка — чётные.

Запуск (внутри контейнера):

    docker exec -it orderflow_general python -m app.scripts.volume_burst_precision \\
        --days 2024-10-01,...,2026-09-01
"""
import argparse
import glob
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd

from app.scripts.impulse_express import (
    AGG_TRADES_DIR,
    BUCKETS_PER_DAY,
    BUCKETS_PER_SECOND,
    DEFAULT_HISTORY_DIR,
    GRID_MS,
    first_crossings,
)
from app.scripts.orderflow_pre_impulse import t_two_sided_p

WINDOW_MS = 500
BASELINE_SEC = 300
RATIOS = (2, 3, 5, 10, 20)
MIN_TRADES = (1, 3, 10)
SIGNAL_COOLDOWN_SEC = 30

IMPULSE_WINDOW_SEC = 2
SQUEEZE_MOVES = (1.5, 2.0)
# Окно признака в разделе 15 стояло за 0.5–1.5 с до T0, а сам ход длится до
# 2 с: сработать он может до 3.5 с после сигнала.
SQUEEZE_HORIZON_MS = 3500
EVENT_COOLDOWN_SEC = 180

ENTRY_LAG_MS = 200
HOLDS_SECONDS = (5, 30, 180)
COST_PERCENT = 0.15
# База по модулю хода считается на каждом `BASE_STRIDE`-м такте: хватает
# с запасом, а весь день — лишняя минута на файл.
BASE_STRIDE = 20
MIN_SIGNALS = 500

W = WINDOW_MS // GRID_MS
BASE = BASELINE_SEC * BUCKETS_PER_SECOND
HORIZON = SQUEEZE_HORIZON_MS // GRID_MS
LAG = ENTRY_LAG_MS // GRID_MS
SPAN = IMPULSE_WINDOW_SEC * BUCKETS_PER_SECOND
REACH = LAG + max(HOLDS_SECONDS) * BUCKETS_PER_SECOND + 1


def day_arrays(path: Path):
    """Сетка 100 мс: последняя цена, объём в USDT, CVD в USDT, число сделок."""
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

    day_start = frame["transact_time"].iloc[0] // 86_400_000 * 86_400_000
    bucket = (frame["transact_time"].to_numpy() - day_start) // GRID_MS
    keep = (bucket >= 0) & (bucket < BUCKETS_PER_DAY)
    bucket = bucket[keep]
    price = frame["price"].to_numpy()[keep]
    quote = price * frame["quantity"].to_numpy()[keep]
    maker = frame["is_buyer_maker"].astype(str).str.lower().to_numpy()[keep] == "true"

    volume = np.bincount(bucket, weights=quote, minlength=BUCKETS_PER_DAY)
    signed = np.bincount(bucket, weights=np.where(maker, -quote, quote),
                         minlength=BUCKETS_PER_DAY)
    trades = np.bincount(bucket, minlength=BUCKETS_PER_DAY)

    # Последняя цена такта: сделки идут по времени, последняя запись
    # такта перезаписывает предыдущие.
    last = np.full(BUCKETS_PER_DAY, np.nan)
    last[bucket] = price
    known = ~np.isnan(last)
    index = np.where(known, np.arange(BUCKETS_PER_DAY), 0)
    np.maximum.accumulate(index, out=index)
    last = last[index]
    last[:np.argmax(known)] = np.nan

    return last, volume, signed, trades


def window_sum(values: np.ndarray, width: int) -> np.ndarray:
    """out[b] = сумма values[b − width : b]; для b < width — NaN."""
    cum = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
    out = np.full(values.size, np.nan)
    out[width:] = cum[width:values.size] - cum[:values.size - width]
    return out


def debounce(positions: np.ndarray, cooldown: int) -> np.ndarray:
    """Первые срабатывания не ближе `cooldown`: прыжок по searchsorted.

    То же, что `first_crossings`, но число шагов равно числу сигналов, а
    не числу тактов, где условие выполнено.
    """
    kept = []
    i = 0

    while i < positions.size:
        kept.append(positions[i])
        i = int(np.searchsorted(positions, positions[i] + cooldown, side="left"))

    return np.array(kept, dtype=np.int64)


def any_ahead(flags: np.ndarray, horizon: int) -> np.ndarray:
    """out[b] — есть ли `True` в flags[b + 1 : b + 1 + horizon]."""
    cum = np.concatenate(([0], np.cumsum(flags.astype(np.int64))))
    b = np.arange(flags.size)
    hi = np.minimum(b + 1 + horizon, flags.size)
    lo = np.minimum(b + 1, flags.size)
    return (cum[hi] - cum[lo]) > 0


def process_day(task):
    symbol, day, path = task
    arrays = day_arrays(Path(path))

    if arrays is None:
        return []

    last, volume, signed, trades = arrays
    v_win = window_sum(volume, W)
    cvd_win = window_sum(signed, W)
    n_win = window_sum(trades.astype(np.float64), W)
    base = (window_sum(volume, BASE + W) - v_win) / ((BASE) / W)

    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = np.where(base > 0, v_win / base, np.nan)
        change = np.full(BUCKETS_PER_DAY, np.nan)
        change[SPAN:] = (last[SPAN:] / last[:-SPAN] - 1) * 100

    valid = np.zeros(BUCKETS_PER_DAY, dtype=bool)
    valid[BASE + W:BUCKETS_PER_DAY - REACH] = True
    valid &= ~np.isnan(last)

    moved = {m: np.abs(np.nan_to_num(change)) >= m for m in SQUEEZE_MOVES}
    up = {m: np.nan_to_num(change) >= m for m in SQUEEZE_MOVES}
    down = {m: np.nan_to_num(change) <= -m for m in SQUEEZE_MOVES}
    ahead = {m: any_ahead(moved[m], HORIZON) for m in SQUEEZE_MOVES}
    ahead_up = any_ahead(up[1.5], HORIZON)
    ahead_down = any_ahead(down[1.5], HORIZON)

    # Сквизы с паузой — для полноты, как события в orderflow_pre_impulse.
    events = {}
    for m in SQUEEZE_MOVES:
        fired = moved[m] & valid
        events[m] = first_crossings(fired, EVENT_COOLDOWN_SEC * BUCKETS_PER_SECOND)

    rows = []
    base_idx = np.flatnonzero(valid)[::BASE_STRIDE]
    base_row = {"symbol": symbol, "day": day, "config": "база",
                "n": int(valid.sum()),
                "sq15": int(ahead[1.5][valid].sum()),
                "sq20": int(ahead[2.0][valid].sum()),
                "events15": len(events[1.5]), "events20": len(events[2.0])}

    for hold in HOLDS_SECONDS:
        entry = last[base_idx + LAG]
        exit_ = last[base_idx + LAG + hold * BUCKETS_PER_SECOND]
        move = np.abs(exit_ / entry - 1) * 100
        base_row[f"abs{hold}_sum"] = float(np.nansum(move))
        base_row[f"abs{hold}_n"] = int(np.sum(~np.isnan(move)))

    rows.append(base_row)

    for k, n_min in product(RATIOS, MIN_TRADES):
        cond = valid & (ratio >= k) & (n_win >= n_min)
        signals = debounce(np.flatnonzero(cond),
                           SIGNAL_COOLDOWN_SEC * BUCKETS_PER_SECOND)
        row = {"symbol": symbol, "day": day, "config": f"K{k}_N{n_min}",
               "n": len(signals)}

        if not len(signals):
            rows.append(row)
            continue

        direction = np.sign(cvd_win[signals])
        row["sq15"] = int(ahead[1.5][signals].sum())
        row["sq20"] = int(ahead[2.0][signals].sum())
        row["dsq15"] = int(np.sum(np.where(direction > 0, ahead_up[signals],
                                           ahead_down[signals]) & (direction != 0)))
        row["n_dir"] = int(np.sum(direction != 0))

        # Полнота: сквиз, перед которым в пределах горизонта был сигнал.
        marks = np.zeros(BUCKETS_PER_DAY, dtype=np.int64)
        marks[signals] = 1
        cum = np.concatenate(([0], np.cumsum(marks)))

        for m, key in ((1.5, "covered15"), (2.0, "covered20")):
            ev = events[m]
            lo = np.maximum(ev - HORIZON, 0)
            row[key] = int(np.sum(cum[ev] - cum[lo] > 0))

        for hold in HOLDS_SECONDS:
            entry = last[signals + LAG]
            exit_ = last[signals + LAG + hold * BUCKETS_PER_SECOND]
            move = (exit_ / entry - 1) * 100
            signed_move = move * direction
            ok = (direction != 0) & ~np.isnan(signed_move)
            row[f"r{hold}_sum"] = float(signed_move[ok].sum())
            row[f"r{hold}_n"] = int(ok.sum())
            row[f"abs{hold}_sum"] = float(np.nansum(np.abs(move)))
            row[f"abs{hold}_n"] = int(np.sum(~np.isnan(move)))

        rows.append(row)

    return rows


def clustered_mean(sums: np.ndarray, counts: np.ndarray) -> tuple:
    """Среднее по наблюдениям и t с кластеризацией по дню (CR1).

    Для одного среднего сэндвич сводится к остаткам по кластеру:
    Var = G/(G−1) · Σ(S_g − n_g·μ)² / N². Суммы и число наблюдений по
    дню — всё, что для этого нужно.
    """
    keep = counts > 0
    sums, counts = sums[keep], counts[keep]
    total = counts.sum()
    groups = keep.sum()

    if total == 0 or groups < 2:
        return np.nan, np.nan, np.nan

    mean = sums.sum() / total
    resid = sums - counts * mean
    var = groups / (groups - 1) * np.sum(resid ** 2) / total ** 2
    se = np.sqrt(var)
    return mean, se, groups


def summarize(frame: pd.DataFrame) -> pd.DataFrame:
    by_day = frame.groupby(["config", "day"]).sum(numeric_only=True).reset_index()
    base = by_day[by_day["config"] == "база"].set_index("day")
    rows = []

    for config, g in by_day[by_day["config"] != "база"].groupby("config"):
        days = g["day"].to_numpy()
        n = g["n"].to_numpy()
        b = base.loc[days]
        row = {"конфигурация": config, "сигналов": int(n.sum()),
               "дней": int((n > 0).sum())}
        pair_days = int((frame["config"] == config).sum())
        row["сигналов на день пары"] = n.sum() / max(pair_days, 1)

        for key, label in (("sq15", "≥1.5%"), ("sq20", "≥2%")):
            precision = g[key].sum() / n.sum() if n.sum() else np.nan
            base_rate = b[key].sum() / b["n"].sum()
            row[f"точность {label}"] = precision
            row[f"база {label}"] = base_rate
            row[f"подъём {label}"] = precision / base_rate if base_rate else np.nan

        row["точность ≥1.5% по CVD"] = (g["dsq15"].sum() / g["n_dir"].sum()
                                        if g["n_dir"].sum() else np.nan)
        row["полнота ≥1.5%"] = g["covered15"].sum() / b["events15"].sum()
        row["полнота ≥2%"] = g["covered20"].sum() / b["events20"].sum()

        for hold in HOLDS_SECONDS:
            mean, se, groups = clustered_mean(
                g[f"r{hold}_sum"].to_numpy(), g[f"r{hold}_n"].to_numpy())
            net = mean - COST_PERCENT
            t_net = net / se if se else np.nan
            row[f"валовое {hold}с"] = mean
            row[f"нетто {hold}с"] = net
            row[f"t нетто {hold}с"] = t_net
            row[f"p нетто {hold}с"] = (
                t_two_sided_p(t_net, int(groups) - 1) if np.isfinite(groups)
                else np.nan
            )
            row[f"|ход| {hold}с"] = g[f"abs{hold}_sum"].sum() / g[f"abs{hold}_n"].sum()
            row[f"|ход| база {hold}с"] = b[f"abs{hold}_sum"].sum() / b[f"abs{hold}_n"].sum()

        rows.append(row)

    return pd.DataFrame(rows)


def half(day: str) -> str:
    return "подбор" if int(day[5:7]) % 2 == 1 else "проверка"


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--tag", default="")
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR))
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    tasks = []

    for day in days:
        for path in sorted(glob.glob(str(
            root / AGG_TRADES_DIR.format(symbol="*") / f"*-aggTrades-{day}.zip"
        ))):
            tasks.append((Path(path).parent.name, day, path))

    print(f"Дней пар: {len(tasks)}", flush=True)
    rows = []

    with ProcessPoolExecutor(args.workers) as pool:
        for done, day_rows in enumerate(pool.map(process_day, tasks, chunksize=8), 1):
            rows.extend(day_rows)

            if done % 1000 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    frame = pd.DataFrame(rows).fillna(0)
    frame["половина"] = frame["day"].map(half)
    out = root / "analysis" / f"volume_burst_precision{('_' + args.tag) if args.tag else ''}"
    frame.to_csv(f"{out}_by_pair_day.csv.gz", index=False)

    tables = []
    for name, part in frame.groupby("половина"):
        table = summarize(part)
        table.insert(0, "половина", name)
        tables.append(table)

    result = pd.concat(tables)
    result.to_csv(f"{out}.csv", index=False)

    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 60)
    pd.set_option("display.max_rows", 100)
    cols = ["половина", "конфигурация", "сигналов", "сигналов на день пары",
            "точность ≥1.5%", "база ≥1.5%", "подъём ≥1.5%", "полнота ≥1.5%",
            "нетто 5с", "нетто 30с", "t нетто 30с", "нетто 180с"]
    print(result[cols].to_string(index=False, float_format=lambda v: f"{v:.4g}"))


if __name__ == "__main__":
    main()
