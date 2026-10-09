"""Сплошной прогон лонга провала за несколько месяцев с портфельными фильтрами.

Запускается руками, в `supervisord.conf` не добавляется.

Ячейка та же, что в разделах 14.3 и 17 плана
(`.ai/docs/test-bots/15-orderflow-plan.md`): провал цены ≥2% за ≤1 с,
рыночный лонг через 200 мс, выход строго через 180 с, издержки 0.15% за
круг. Параметры не крутятся — их нет в аргументах намеренно.

Чем отличается от `squeeze_express.py`, который считал ту же ячейку:

* **Вселенная — топ-100 по объёму** (`TOP_N`) с объёмом больше
  `MIN_QUOTE_VOLUME_USD`, а не все пары. Объём — оборот в USDT
  (`price * quantity` из тех же aggTrades) за **предыдущие** UTC-сутки:
  объём дня сигнала включает сам каскад и подсматривает вперёд. Поэтому
  прогон читает и день перед первым месяцем — только ради объёма;
* **кулдаун 180 с на пару отсчитывается от взятой сделки**, а не от
  любого сквиза: у `squeeze_express` выброс вверх тоже занимал паре
  окно, здесь шорта нет;
* **не больше `MAX_OPEN` позиций одновременно на весь портфель.** Это
  уже не по парам, а по времени, поэтому сначала собираются все такты,
  где условие провала выполнено (`process_day`), а сделки отбираются
  потом, в хронологическом порядке по всей вселенной (`select_trades`).
  Позиция занимает слот от сигнала до выхода. Сигнал, пропущенный из-за
  лимита, кулдаун не запускает: если условие ещё держится, когда слот
  освободится, вход будет по тому такту;
* в одном такте 100 мс на каскаде срабатывают десятки пар, а слота два.
  Порядок внутри такта — **по объёму предыдущих суток**, ликвидные первыми:
  мгновения внутри такта сетка не различает, а выбор по размеру провала
  или по результату подсматривал бы.

Модель исполнения прежняя (`impulse_express.day_grid`): цена такта —
последняя сделка, вход по ней через 200 мс, выход по последней сделке
через 180 с после входа. Сигналы, выход которых не помещается в сутки,
отброшены, как и в `squeeze_express`.

Статистика — по сделкам, ошибка среднего с кластеризацией по
календарным дням (`clustered_stats`): сделки одного дня — часто один
каскад, по отдельности они не независимы.

Запуск — одноразовым контейнером, потому что июнь и июль лежат не в
смонтированном архиве. Каталоги перечисляются через запятую, файл пары
за день берётся из первого каталога, где он есть:

    docker run --rm \\
      -v ~/binance-history:/data/binance-history \\
      -v $PWD/data/binance/aggtrades:/data/aggtrades-2:ro \\
      -v $PWD/general:/opt/services/app/src \\
      -w /opt/services/app/src marketmaker-orderflow-general \\
      python -m app.scripts.dip_long_oos \\
        --roots /data/aggtrades-2,/data/binance-history \\
        --months 2026-06,2026-07,2026-08,2026-09

Первый проход долгий (десятки тысяч архивов); его результат пишется в
`analysis/dip_long_oos_candidates.csv.gz` и `..._volumes.csv.gz`
основного архива, и `--reuse` берёт
их вместо повторного чтения архивов.
"""
import argparse
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.scripts.impulse_express import (
    AGG_TRADES_DIR,
    BUCKETS_PER_DAY,
    BUCKETS_PER_SECOND,
    DEFAULT_HISTORY_DIR,
    GRID_MS,
    grid_from_trades,
    read_day_trades,
)

WINDOW_SECONDS = 1
DROP_PERCENT = 2.0
ENTRY_LAG_MS = 200
HOLD_SECONDS = 180
COST_PERCENT = 0.15

TOP_N = 100
MIN_QUOTE_VOLUME_USD = 20_000_000
COOLDOWN_SECONDS = 180
MAX_OPEN = 2

ENTRY_LAG_BUCKETS = ENTRY_LAG_MS // GRID_MS
HOLD_BUCKETS = HOLD_SECONDS * BUCKETS_PER_SECOND
COOLDOWN_BUCKETS = COOLDOWN_SECONDS * BUCKETS_PER_SECOND


def process_day(task):
    """Оборот пары за сутки и все такты с провалом, с валовым итогом входа.

    Возвращает (symbol, day, оборот USDT, список (такт, провал %, валовое %)).
    """
    symbol, day, path = task
    trades = read_day_trades(path, extra_columns=("quantity",))
    volume = float((trades["price"] * trades["quantity"]).sum())
    grid = grid_from_trades(trades)

    if grid is None:
        return symbol, day, volume, []

    last = grid[0]
    span = WINDOW_SECONDS * BUCKETS_PER_SECOND
    reach = ENTRY_LAG_BUCKETS + HOLD_BUCKETS + 1

    with np.errstate(invalid="ignore"):
        change = np.full(BUCKETS_PER_DAY, np.nan)
        change[span:] = (last[span:] / last[:-span] - 1) * 100

    fired = np.nan_to_num(change) <= -DROP_PERCENT
    fired[BUCKETS_PER_DAY - reach:] = False
    triggers = np.flatnonzero(fired)
    entry = last[triggers + ENTRY_LAG_BUCKETS]
    exit_ = last[triggers + ENTRY_LAG_BUCKETS + HOLD_BUCKETS]
    gross = (exit_ / entry - 1) * 100
    rows = list(zip(
        triggers.tolist(), (-change[triggers]).tolist(), gross.tolist(),
    ))

    return symbol, day, volume, rows


def eligible_pairs(volumes: pd.DataFrame) -> pd.DataFrame:
    """Пары, допущенные к торговле в день `day`, с объёмом прошлых суток.

    Топ-`TOP_N` по обороту за предыдущий календарный день и оборот выше
    `MIN_QUOTE_VOLUME_USD`. Колонки: symbol, day, prev_volume.
    """
    frame = volumes.copy()
    frame["day"] = (
        pd.to_datetime(frame["day"]) + pd.Timedelta(days=1)
    ).dt.strftime("%Y-%m-%d")
    frame = frame[frame["quote_volume"] > MIN_QUOTE_VOLUME_USD]
    frame = frame.sort_values(["day", "quote_volume"], ascending=[True, False])
    frame = frame.groupby("day").head(TOP_N)
    return frame.rename(columns={"quote_volume": "prev_volume"})[
        ["symbol", "day", "prev_volume"]
    ]


def select_trades(candidates: pd.DataFrame) -> pd.DataFrame:
    """Отбор сделок по времени: кулдаун пары и лимит открытых позиций.

    `candidates` — такты с провалом у допущенных пар, колонки symbol, day,
    bucket, prev_volume и прочие. Возвращает взятые строки.
    """
    if candidates.empty:
        return candidates

    frame = candidates.copy()
    day_index = (
        pd.to_datetime(frame["day"]) - pd.Timestamp("1970-01-01")
    ).dt.days.to_numpy()
    frame["time"] = day_index.astype(np.int64) * BUCKETS_PER_DAY + frame["bucket"]
    frame = frame.sort_values(
        ["time", "prev_volume", "symbol"], ascending=[True, False, True],
    )

    open_until: list[int] = []
    allowed_from: dict[str, int] = {}
    taken = []

    for row in frame.itertuples():
        open_until = [end for end in open_until if end > row.time]

        if allowed_from.get(row.symbol, -1) > row.time:
            continue

        if len(open_until) >= MAX_OPEN:
            continue

        open_until.append(row.time + ENTRY_LAG_BUCKETS + HOLD_BUCKETS)
        allowed_from[row.symbol] = row.time + COOLDOWN_BUCKETS
        taken.append(row.Index)

    return frame.loc[taken].drop(columns="time")


def t_critical_975(df: int) -> float:
    """Квантиль 0.975 распределения Стьюдента (Корниш–Фишер).

    В образе нет scipy. При df ≥ 20 расходится с точным значением меньше
    чем на 0.001.
    """
    z = 1.959963984540054
    return (
        z
        + (z**3 + z) / (4 * df)
        + (5 * z**5 + 16 * z**3 + 3 * z) / (96 * df**2)
        + (3 * z**7 + 19 * z**5 + 17 * z**3 - 15 * z) / (384 * df**3)
    )


def clustered_stats(values: np.ndarray, clusters: np.ndarray):
    """Среднее, его ошибка с кластеризацией (CR1), t и 95% ДИ.

    Ошибка — сэндвич по кластерам с поправкой G/(G-1), критическое
    значение — Стьюдент с G-1 степенями свободы.
    """
    mean = values.mean()
    _, inverse = np.unique(clusters, return_inverse=True)
    groups = inverse.max() + 1

    if groups < 2:
        return mean, np.nan, np.nan, np.nan, np.nan

    residual_sums = np.bincount(inverse, weights=values - mean)
    variance = (
        groups / (groups - 1) * (residual_sums**2).sum() / values.size**2
    )
    error = np.sqrt(variance)
    t = mean / error if error > 0 else np.nan
    half = t_critical_975(groups - 1) * error
    return mean, error, t, mean - half, mean + half


def summarize(trades: pd.DataFrame, label: str, calendar_days: int) -> dict:
    net = trades["net"].to_numpy()
    days = trades["day"].to_numpy()
    mean, _, t, low, high = clustered_stats(net, days)
    daily = trades.groupby("day")["net"].sum()
    total = net.sum()
    top3 = np.sort(net)[-3:].sum() if net.size >= 3 else np.nan

    return {
        "период": label,
        "сделок": net.size,
        "пар": trades["symbol"].nunique(),
        "дней со сделками": f"{daily.size}/{calendar_days}",
        "валовое": trades["gross"].mean(),
        "Net EV": mean,
        "95% ДИ (дни)": f"[{low:+.3f}; {high:+.3f}]",
        "t (дни)": t,
        "дней в плюсе": f"{(daily > 0).sum()}/{daily.size} "
                        f"({100 * (daily > 0).mean():.0f}%)",
        "винрейт": 100 * (net > 0).mean(),
        "медиана": np.median(net),
        "топ-3, % суммы": 100 * top3 / total if total > 0 else np.nan,
        "без топ-3": np.sort(net)[:-3].mean() if net.size > 3 else np.nan,
    }


def month_days(month: str) -> list[date]:
    first = date.fromisoformat(f"{month}-01")
    following = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return [first + timedelta(days=i) for i in range((following - first).days)]


def collect_tasks(roots: list[Path], days: list[date]):
    """(symbol, day, path) по всем каталогам; первый каталог главнее."""
    wanted = {day.isoformat() for day in days}
    found: dict[tuple[str, str], Path] = {}

    for root in roots:
        base = root / AGG_TRADES_DIR.format(symbol="")

        if not base.exists():
            continue

        for folder in base.iterdir():
            for path in folder.glob(f"{folder.name}-aggTrades-*.zip"):
                day = path.name[-14:-4]

                if day in wanted:
                    found.setdefault((folder.name, day), path)

    return [(symbol, day, path) for (symbol, day), path in sorted(found.items())]


def first_pass(tasks, workers: int):
    volumes, candidates = [], []

    with ProcessPoolExecutor(workers) as pool:
        for done, (symbol, day, volume, rows) in enumerate(
            pool.map(process_day, tasks, chunksize=4), 1,
        ):
            volumes.append((symbol, day, volume))
            candidates.extend(
                (symbol, day, bucket, drop, gross)
                for bucket, drop, gross in rows
            )

            if done % 2000 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    return (
        pd.DataFrame(volumes, columns=["symbol", "day", "quote_volume"]),
        pd.DataFrame(
            candidates, columns=["symbol", "day", "bucket", "drop", "gross"],
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--roots",
        default=os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR),
        help="каталоги выгрузки через запятую",
    )
    parser.add_argument("--months", default="2026-06,2026-07,2026-08,2026-09")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--out", default=f"{DEFAULT_HISTORY_DIR}/analysis/dip_long_oos",
        help="префикс файлов результата",
    )
    parser.add_argument(
        "--reuse", action="store_true",
        help="взять первый проход из <out>_*.csv.gz вместо архивов",
    )
    args = parser.parse_args()

    roots = [Path(r.strip()) for r in args.roots.split(",") if r.strip()]
    months = [m.strip() for m in args.months.split(",") if m.strip()]
    out = args.out
    trade_days = [day for month in months for day in month_days(month)]
    # День перед первым — только ради объёма для фильтра первого дня.
    read_days = [trade_days[0] - timedelta(days=1)] + trade_days

    if args.reuse:
        volumes = pd.read_csv(f"{out}_volumes.csv.gz")
        candidates = pd.read_csv(f"{out}_candidates.csv.gz")
    else:
        tasks = collect_tasks(roots, read_days)
        print(f"файлов {len(tasks)}, пар "
              f"{len({symbol for symbol, _, _ in tasks})}", flush=True)
        volumes, candidates = first_pass(tasks, args.workers)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        volumes.to_csv(f"{out}_volumes.csv.gz", index=False)
        candidates.to_csv(f"{out}_candidates.csv.gz", index=False)

    coverage = volumes.groupby("day").size()
    print("\nфайлов по дням: мин {} ({}), макс {}".format(
        coverage.min(), coverage.idxmin(), coverage.max(),
    ))
    missing = sorted({d.isoformat() for d in read_days} - set(coverage.index))

    if missing:
        print(f"нет ни одного файла за: {', '.join(missing)}")

    eligible = eligible_pairs(volumes)
    trade_day_set = {d.isoformat() for d in trade_days}
    eligible = eligible[eligible["day"].isin(trade_day_set)]
    per_day = eligible.groupby("day").size()
    print(f"допущено пар в день: мин {per_day.min()}, медиана "
          f"{per_day.median():.0f}, макс {per_day.max()}; "
          f"всего разных {eligible['symbol'].nunique()}")

    pool = candidates.merge(eligible, on=["symbol", "day"])
    trades = select_trades(pool)
    trades["net"] = trades["gross"] - COST_PERCENT
    trades["month"] = trades["day"].str[:7]
    trades.to_csv(f"{out}_trades.csv", index=False)

    print(f"тактов с провалом у допущенных пар {len(pool)}, "
          f"взято сделок {len(trades)}")

    rows = [
        summarize(trades[trades["month"] == month], month, len(month_days(month)))
        for month in months
    ]
    rows.append(summarize(trades, "итого", len(trade_days)))
    table = pd.DataFrame(rows)
    table.to_csv(f"{out}.csv", index=False)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:+.3f}")
    print(f"\nЛонг провала ≥{DROP_PERCENT}%/{WINDOW_SECONDS}с, вход +"
          f"{ENTRY_LAG_MS} мс, выход +{HOLD_SECONDS} с, издержки "
          f"{COST_PERCENT}%, топ-{TOP_N} > ${MIN_QUOTE_VOLUME_USD / 1e6:.0f}M, "
          f"кулдаун {COOLDOWN_SECONDS} с, не больше {MAX_OPEN} позиций; %:")
    print(table.to_string(index=False))

    top = trades.nlargest(10, "net")[["day", "symbol", "bucket", "drop", "net"]]
    top["time"] = pd.to_timedelta(top["bucket"] * GRID_MS, unit="ms")
    print("\nДесять лучших сделок:")
    print(top.drop(columns="bucket").to_string(index=False))


if __name__ == "__main__":
    main()
