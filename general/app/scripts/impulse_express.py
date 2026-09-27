"""Экспресс-анализ секундных импульсов по августовским aggTrades.

Запускается руками, в `supervisord.conf` не добавляется. Отвечает на три
вопроса по топу пар по числу сделок (`analysis/trade_counts.csv`):

1. **Импульс.** Сколько раз цена проходит `>= 0.3%` и `>= 0.5%` за одну
   секунду. Каждый порог ищется отдельно: событие по 0.5% — это первое
   пересечение именно 0.5%, а не подмножество событий по 0.3% со своим
   моментом входа.
2. **Что дальше.** Ход цены в сторону импульса на `+1/+3/+5/+10 с`,
   доля продолжения (ход > 0), разворота (ход < 0) и глубокого отката
   (вернул половину импульса и больше).
3. **Матрица профита.** Два правила с фиксированными TP/SL и выходом по
   таймауту, комиссия `COMMISSION_ROUND_TRIP_PERCENT` за круг:
   против импульса (TP 0.4 / SL 0.3) и по импульсу (TP 0.5 / SL 0.3).

Чем отличается от `orderflow_thresholds.py`: тот считает распределения
по всей сетке стратегии, этот — точечная проверка двух готовых правил
с TP/SL по пути цены. Сетка своя — 100 мс, а не такт бота.

**Допущения, от которых зависит ответ.**

* Цена такта — последняя сделка, для касания TP/SL берутся максимум и
  минимум сделок такта. Если в одном такте задеты оба уровня, считается
  стоп: порядок внутри 100 мс неизвестен, и выбор худшего не даёт
  нарисовать прибыль на неразличимом.
* Выход по TP и SL — ровно по уровню, без проскальзывания. Для стопа это
  оптимистично: на импульсе стоп исполняется хуже уровня.
* Вход через `ENTRY_LAGS_MS` после триггера. Лаг 0 — теоретический
  потолок, 200 мс — реальная задержка от решения до заявки; разница
  между ними и есть цена скорости.
* События одной пары не перекрываются: следующий триггер не раньше
  `EVENT_COOLDOWN_SECONDS`, то есть самого длинного удержания.
* Комиссия 0.15% за круг включает спред: отдельно спред не вычитается.

Запуск (внутри контейнера, туда смонтирован архив):

    docker exec -it orderflow_general python -m app.scripts.impulse_express
    docker exec -it orderflow_general python -m app.scripts.impulse_express --top 5 --days 3
"""
import argparse
import os
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

GRID_MS = 100
BUCKETS_PER_SECOND = 1000 // GRID_MS
BUCKETS_PER_DAY = 24 * 60 * 60 * BUCKETS_PER_SECOND

WINDOW_SECONDS = 1
MOVE_PERCENTS = (0.3, 0.5)
HORIZONS_SECONDS = (1, 3, 5, 10)
ENTRY_LAGS_MS = (0, 200)

# Максимальное удержание сделки. 10 с — самый длинный горизонт вопроса,
# 30 с — проверка, не обрезает ли таймаут отработку TP.
HOLD_SECONDS = (10, 30)
EVENT_COOLDOWN_SECONDS = max(HOLD_SECONDS)

COMMISSION_ROUND_TRIP_PERCENT = 0.15

# Направление входа относительно импульса: -1 — против, +1 — по нему.
MODELS = {
    "mean_reversion": {"side": -1, "tp": 0.4, "sl": 0.3},
    "momentum": {"side": 1, "tp": 0.5, "sl": 0.3},
}

# Глубокий откат — цена вернула не меньше этой доли импульса.
DEEP_PULLBACK_SHARE = 0.5

DEFAULT_HISTORY_DIR = "/data/binance-history"
AGG_TRADES_DIR = "data/futures/um/daily/aggTrades/{symbol}"
MONTH = "2026-08"


def day_grid(path: Path):
    """Сетка 100 мс за сутки: последняя цена, максимум и минимум такта.

    Пустой такт получает последнюю известную цену во все три поля:
    сделок не было, значит, и касания уровней тоже.
    """
    with zipfile.ZipFile(path) as archive:
        with archive.open(archive.namelist()[0]) as handle:
            frame = pd.read_csv(
                handle, usecols=["price", "transact_time"],
                dtype={"price": np.float64, "transact_time": np.int64},
            )

    if frame.empty:
        return None

    day_start = frame["transact_time"].iloc[0] // 86_400_000 * 86_400_000
    bucket = (frame["transact_time"].to_numpy() - day_start) // GRID_MS
    frame["bucket"] = bucket
    frame = frame[(bucket >= 0) & (bucket < BUCKETS_PER_DAY)]

    grouped = frame.groupby("bucket", sort=True)["price"]
    last = np.full(BUCKETS_PER_DAY, np.nan)
    high = np.full(BUCKETS_PER_DAY, np.nan)
    low = np.full(BUCKETS_PER_DAY, np.nan)
    index = grouped.last().index.to_numpy()
    last[index] = grouped.last().to_numpy()
    high[index] = grouped.max().to_numpy()
    low[index] = grouped.min().to_numpy()

    known = ~np.isnan(last)
    positions = np.where(known, np.arange(BUCKETS_PER_DAY), 0)
    np.maximum.accumulate(positions, out=positions)
    last = last[positions]
    empty = ~known
    high[empty] = last[empty]
    low[empty] = last[empty]
    last[:np.argmax(known)] = np.nan

    return last, high, low


def first_crossings(fired: np.ndarray, cooldown: int) -> np.ndarray:
    """Начала срабатываний, разнесённые не ближе чем на `cooldown`."""
    kept = []
    next_allowed = -1

    for position in np.flatnonzero(fired):
        if position >= next_allowed:
            kept.append(position)
            next_allowed = position + cooldown

    return np.array(kept, dtype=np.int64)


def simulate(entry, side, tp, sl, high, low, last, start, hold):
    """Сделка с TP/SL по пути цены. Возвращает (валовый %, исход)."""
    stop = start + hold
    up = (high[start:stop] / entry - 1) * 100 * side
    down = (low[start:stop] / entry - 1) * 100 * side

    # Для лонга выгодный край — максимум, для шорта — минимум.
    favorable = up if side > 0 else down
    adverse = down if side > 0 else up

    tp_hit = np.flatnonzero(favorable >= tp)
    sl_hit = np.flatnonzero(adverse <= -sl)
    tp_at = tp_hit[0] if tp_hit.size else hold
    sl_at = sl_hit[0] if sl_hit.size else hold

    if sl_at < hold and sl_at <= tp_at:
        return -sl, "sl"

    if tp_at < hold:
        return tp, "tp"

    return (last[stop - 1] / entry - 1) * 100 * side, "timeout"


def process_day(task):
    symbol, path = task
    grid = day_grid(path)

    if grid is None:
        return []

    last, high, low = grid
    span = WINDOW_SECONDS * BUCKETS_PER_SECOND
    lags = [lag // GRID_MS for lag in ENTRY_LAGS_MS]
    holds = [h * BUCKETS_PER_SECOND for h in HOLD_SECONDS]
    horizons = [h * BUCKETS_PER_SECOND for h in HORIZONS_SECONDS]
    reach = max(lags) + max(max(holds), max(horizons)) + 1

    with np.errstate(invalid="ignore"):
        change = np.full(BUCKETS_PER_DAY, np.nan)
        change[span:] = (last[span:] / last[:-span] - 1) * 100

    rows = []
    cooldown = EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND

    for move in MOVE_PERCENTS:
        fired = np.abs(np.nan_to_num(change)) >= move
        fired[BUCKETS_PER_DAY - reach:] = False

        for trigger in first_crossings(fired, cooldown):
            direction = 1 if change[trigger] > 0 else -1
            row = {
                "symbol": symbol,
                "day": path.name[-14:-4],
                "bucket": int(trigger),
                "move_threshold": move,
                "direction": direction,
                "impulse": abs(change[trigger]),
            }

            for lag_ms, lag in zip(ENTRY_LAGS_MS, lags):
                entry_at = trigger + lag
                entry = last[entry_at]
                row[f"slip_lag{lag_ms}"] = (
                    (entry / last[trigger] - 1) * 100 * direction
                )

                for seconds, h in zip(HORIZONS_SECONDS, horizons):
                    row[f"r{seconds}_lag{lag_ms}"] = (
                        (last[entry_at + h] / entry - 1) * 100 * direction
                    )

                for name, model in MODELS.items():
                    side = direction * model["side"]

                    for seconds, hold in zip(HOLD_SECONDS, holds):
                        gross, outcome = simulate(
                            entry, side, model["tp"], model["sl"],
                            high, low, last, entry_at + 1, hold,
                        )
                        key = f"{name}_h{seconds}_lag{lag_ms}"
                        row[f"{key}_net"] = (
                            gross - COMMISSION_ROUND_TRIP_PERCENT
                        )
                        row[f"{key}_exit"] = outcome

            rows.append(row)

    return rows


def pick_symbols(root: Path, top: int) -> list[str]:
    counts = pd.read_csv(root / "analysis" / "trade_counts.csv")
    counts = counts.sort_values("trades", ascending=False)
    return counts["symbol"].head(top).tolist()


def build_tasks(root: Path, symbols, days):
    tasks = []

    for symbol in symbols:
        folder = root / AGG_TRADES_DIR.format(symbol=symbol)
        files = sorted(folder.glob(f"{symbol}-aggTrades-{MONTH}-*.zip"))
        tasks.extend((symbol, path) for path in files[:days])

    return tasks


def after_impulse_table(events: pd.DataFrame, lag_ms: int) -> pd.DataFrame:
    rows = []

    for move, group in events.groupby("move_threshold"):
        for seconds in HORIZONS_SECONDS:
            r = group[f"r{seconds}_lag{lag_ms}"]
            rows.append({
                "порог": f">={move}%",
                "горизонт": f"+{seconds}с",
                "событий": len(r),
                "продолжение": (r > 0).mean(),
                "разворот": (r < 0).mean(),
                "откат>=50%": (
                    r <= -DEEP_PULLBACK_SHARE * group["impulse"]
                ).mean(),
                "медиана %": r.median(),
                "среднее %": r.mean(),
            })

    return pd.DataFrame(rows)


def ev_table(events: pd.DataFrame) -> pd.DataFrame:
    rows = []

    for move, group in events.groupby("move_threshold"):
        for name, model in MODELS.items():
            for seconds in HOLD_SECONDS:
                for lag_ms in ENTRY_LAGS_MS:
                    key = f"{name}_h{seconds}_lag{lag_ms}"
                    net = group[f"{key}_net"]
                    exits = group[f"{key}_exit"]
                    # Стандартная ошибка по дням пары, а не по сделкам:
                    # импульсы одного дня одной пары — одна серия цен.
                    daily = net.groupby(
                        [group["symbol"], group["day"]]
                    ).mean()
                    per_pair = net.groupby(group["symbol"]).mean()
                    rows.append({
                        "порог": f">={move}%",
                        "модель": name,
                        "TP/SL": f"{model['tp']}/{model['sl']}",
                        "удерж.": f"{seconds}с",
                        "лаг": f"{lag_ms}мс",
                        "сделок": len(net),
                        "винрейт": (net > 0).mean(),
                        "TP": (exits == "tp").mean(),
                        "SL": (exits == "sl").mean(),
                        "таймаут": (exits == "timeout").mean(),
                        "EV нетто %": net.mean(),
                        "EV валовое %": (
                            net.mean() + COMMISSION_ROUND_TRIP_PERCENT
                        ),
                        "t (дни)": daily.mean() / daily.std(ddof=1)
                        * np.sqrt(len(daily)) if len(daily) > 1 else np.nan,
                        "пар EV>0": f"{(per_pair > 0).sum()}/{len(per_pair)}",
                    })

    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--top", type=int, default=35)
    parser.add_argument("--days", type=int, default=31)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--out", default="analysis/impulse_express")
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR))
    symbols = pick_symbols(root, args.top)
    tasks = build_tasks(root, symbols, args.days)
    print(f"пар: {len(symbols)}, файлов: {len(tasks)}", flush=True)

    rows = []

    with ProcessPoolExecutor(args.workers) as pool:
        for done, day_rows in enumerate(
            pool.map(process_day, tasks, chunksize=1), 1,
        ):
            rows.extend(day_rows)

            if done % 50 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    events = pd.DataFrame(rows)
    out = root / args.out
    events.to_csv(f"{out}_events.csv.gz", index=False)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:.4f}")

    print("\nСобытий по порогам и парам (топ-10):")
    counts = events.pivot_table(
        index="symbol", columns="move_threshold", values="bucket",
        aggfunc="count", fill_value=0,
    )
    print(counts.sort_values(MOVE_PERCENTS[0], ascending=False).head(10))
    print("всего:", counts.sum().to_dict())

    for lag_ms in ENTRY_LAGS_MS:
        print(f"\nПоведение после импульса, отсчёт от входа (лаг {lag_ms} мс):")
        print(after_impulse_table(events, lag_ms).to_string(index=False))

    table = ev_table(events)
    table.to_csv(f"{out}_ev.csv", index=False)
    print("\nМатрица EV (комиссия 0.15% за круг):")
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
