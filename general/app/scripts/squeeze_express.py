"""Проверка единственной уцелевшей ячейки: возврат после сквиза ≥1.5–2%.

Запускается руками, в `supervisord.conf` не добавляется.

Раздел 10.5 плана (`.ai/docs/test-bots/15-orderflow-plan.md`) оставил
одну семью правил: шорт выброса вверх ≥2% за 1–2 с, удержание 30 с,
+0.178% нетто. Экспресс-анализ импульсов (`impulse_express.py`) её не
покрывал — там пороги 0.3 и 0.5%, где большая часть событий — скачок
сделки между bid и ask. Здесь — только крупные движения и только вход
против них, с раздельным счётом по направлению: в разделе 10.4 выброс
вверх и провал вниз вели себя противоположно.

**Главное про выборку.** Ячейка найдена на августе. Пересчёт на том же
августе — не проверка, а повтор: он ловит ошибки счёта, но не удачу
отбора. Проверка — это месяц, которого при отборе не видели, поэтому
скрипт принимает `--month` и гоняется дважды. Внутри месяца результат
печатается ещё и по половинам: эффект, который живёт в одной половине,
держится на нескольких днях.

Что меняется относительно `impulse_express.py`:

* вся вселенная, а не топ-35: сквизов ≥2% на паре — единицы в месяц;
* окна 1 и 2 с, пороги 1.5 и 2%, события не ближе 180 с;
* выходы — чистый таймаут (как в разделе 10) и TP/SL в масштабе самих
  сквизов: MFE и MAE там — проценты, а не десятые (раздел 9.5);
* три уровня издержек, см. `COSTS`.

Модель исполнения та же: вход через `ENTRY_LAG_MS` по последней сделке,
касание уровней по максимуму и минимуму такта, стоп при двойном
касании, выход ровно по уровню. **Для «лимитного» уровня издержек это
оптимистично вдвойне**: лимитка против сквиза исполняется тогда, когда
цена идёт против нас, и не исполняется, когда сразу разворачивается, —
а здесь исполнение считается гарантированным.

Запуск (внутри контейнера):

    docker exec -it orderflow_general python -m app.scripts.squeeze_express --month 2026-08
    docker exec -it orderflow_general python -m app.scripts.squeeze_express --month 2026-09
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
    day_grid,
    first_crossings,
    simulate,
)

WINDOWS_SECONDS = (1, 2)
MOVE_PERCENTS = (1.5, 2.0)
ENTRY_LAG_MS = 200
ENTRY_LAG_BUCKETS = ENTRY_LAG_MS // GRID_MS

# Выходы. `tp`/`sl` = None — уровня нет, выход только по таймауту.
# Чистые таймауты повторяют счёт раздела 10 и сверяют с ним числа;
# уровни — от медиан MFE 1.78% и MAE 1.29% из раздела 9.5.
EXITS = (
    {"name": "таймаут 30с", "tp": None, "sl": None, "hold": 30},
    {"name": "таймаут 180с", "tp": None, "sl": None, "hold": 180},
    {"name": "TP0.5/SL1.0 30с", "tp": 0.5, "sl": 1.0, "hold": 30},
    {"name": "TP1.0/SL1.5 30с", "tp": 1.0, "sl": 1.5, "hold": 30},
    {"name": "TP1.0/SL1.5 180с", "tp": 1.0, "sl": 1.5, "hold": 180},
    {"name": "TP1.5/SL3.0 180с", "tp": 1.5, "sl": 3.0, "hold": 180},
)
EVENT_COOLDOWN_SECONDS = max(e["hold"] for e in EXITS)

# Издержки круга, %:
#   limit  - лимитный вход (maker 0.02) и рыночный выход (taker 0.05),
#            как у strategy_1; спред не добавлен — оптимистичная граница;
#   market - обе ноги рыночные, 0.15 со спредом, как в impulse_express;
#   pair   - обе ноги рыночные по 0.05 плюс замеренный спред пары
#            (`analysis/pair_spreads.csv`), как барьер раздела 10.
COST_LIMIT = 0.07
COST_MARKET = 0.15
COMMISSION_TAKER_ROUND_TRIP = 0.10
SPREADS_FILE = "analysis/pair_spreads.csv"

BOOTSTRAP_SAMPLES = 2000


def process_day(task):
    symbol, path = task
    grid = day_grid(path)

    if grid is None:
        return []

    last, high, low = grid
    reach = ENTRY_LAG_BUCKETS + EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND + 1
    cooldown = EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND
    rows = []

    for window in WINDOWS_SECONDS:
        span = window * BUCKETS_PER_SECOND

        with np.errstate(invalid="ignore"):
            change = np.full(BUCKETS_PER_DAY, np.nan)
            change[span:] = (last[span:] / last[:-span] - 1) * 100

        for move in MOVE_PERCENTS:
            fired = np.abs(np.nan_to_num(change)) >= move
            fired[BUCKETS_PER_DAY - reach:] = False

            for trigger in first_crossings(fired, cooldown):
                direction = 1 if change[trigger] > 0 else -1
                # Против импульса: выброс вверх шортим, провал лонгуем.
                side = -direction
                entry_at = trigger + ENTRY_LAG_BUCKETS
                entry = last[entry_at]
                row = {
                    "symbol": symbol,
                    "day": path.name[-14:-4],
                    "bucket": int(trigger),
                    "window": window,
                    "move_threshold": move,
                    "direction": "вверх" if direction > 0 else "вниз",
                    "impulse": abs(change[trigger]),
                }

                for exit_rule in EXITS:
                    tp = exit_rule["tp"] if exit_rule["tp"] else np.inf
                    sl = exit_rule["sl"] if exit_rule["sl"] else np.inf
                    gross, outcome = simulate(
                        entry, side, tp, sl, high, low, last,
                        entry_at + 1, exit_rule["hold"] * BUCKETS_PER_SECOND,
                    )
                    row[f"{exit_rule['name']}|gross"] = gross
                    row[f"{exit_rule['name']}|exit"] = outcome

                rows.append(row)

    return rows


def all_symbols(root: Path, month: str) -> list[str]:
    base = root / AGG_TRADES_DIR.format(symbol="")
    return sorted(
        folder.name for folder in base.iterdir()
        if any(folder.glob(f"*-aggTrades-{month}-*.zip"))
    )


def cluster_bootstrap(values: np.ndarray, clusters: np.ndarray, rng):
    """95% интервал среднего с пересэмплом кластеров (день пары).

    Сквизы одной пары в один день — часто один каскад, и по событиям
    интервал вышел бы уже, чем данные позволяют.
    """
    codes, inverse = np.unique(clusters, return_inverse=True)

    if codes.size < 2:
        return np.nan, np.nan

    sums = np.bincount(inverse, weights=values)
    counts = np.bincount(inverse)
    picks = rng.integers(0, codes.size, (BOOTSTRAP_SAMPLES, codes.size))
    means = sums[picks].sum(axis=1) / counts[picks].sum(axis=1)

    return np.percentile(means, 2.5), np.percentile(means, 97.5)


def summary(events: pd.DataFrame, spreads: dict[str, float]) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    pair_cost = COMMISSION_TAKER_ROUND_TRIP + events["symbol"].map(
        spreads
    ).fillna(np.nanmedian(list(spreads.values())) if spreads else 0.0)
    clusters = (events["symbol"] + "|" + events["day"]).to_numpy()
    first_half = events["day"].str[-2:].astype(int) <= 15
    rows = []

    keys = ["window", "move_threshold", "direction"]

    for (window, move, direction), group in events.groupby(keys):
        index = group.index

        for exit_rule in EXITS:
            name = exit_rule["name"]
            gross = group[f"{name}|gross"]
            exits = group[f"{name}|exit"]
            net_pair = gross - pair_cost[index]
            net_market = gross - COST_MARKET
            low, high = cluster_bootstrap(
                net_market.to_numpy(), clusters[index], rng,
            )
            # Устойчивость к выбросам: среднее без десяти лучших сделок.
            trimmed = net_market.sort_values().iloc[:-10].mean() \
                if len(net_market) > 20 else np.nan
            rows.append({
                "окно": f"{window}с",
                "порог": f">={move}%",
                "импульс": direction,
                "вход": "шорт" if direction == "вверх" else "лонг",
                "выход": name,
                "n": len(gross),
                "пар": group["symbol"].nunique(),
                "валовое": gross.mean(),
                "нетто 0.07": gross.mean() - COST_LIMIT,
                "нетто спред пары": net_pair.mean(),
                "нетто 0.15": net_market.mean(),
                "ДИ 0.15": f"[{low:+.3f}; {high:+.3f}]",
                "без топ-10": trimmed,
                "винрейт 0.15": (net_market > 0).mean(),
                "TP/SL/тайм": "{:.0f}/{:.0f}/{:.0f}".format(
                    *(100 * (exits == k).mean()
                      for k in ("tp", "sl", "timeout"))
                ),
                "валовое 1–15": gross[first_half[index]].mean(),
                "валовое 16–": gross[~first_half[index]].mean(),
            })

    return pd.DataFrame(rows)


def load_spreads(root: Path) -> dict[str, float]:
    path = root / SPREADS_FILE

    if not path.exists():
        print(f"нет {path}: колонка «нетто спред пары» — по медиане 0")
        return {}

    frame = pd.read_csv(path)
    return dict(zip(frame["symbol"], frame["spread_percent"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--month", default="2026-08")
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
    rows = []

    with ProcessPoolExecutor(args.workers) as pool:
        for done, day_rows in enumerate(
            pool.map(process_day, tasks, chunksize=4), 1,
        ):
            rows.extend(day_rows)

            if done % 1000 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    events = pd.DataFrame(rows)
    out = root / "analysis" / f"squeeze_express_{args.month}"
    events.to_csv(f"{out}_events.csv.gz", index=False)

    table = summary(events, load_spreads(root))
    table.to_csv(f"{out}.csv", index=False)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.max_rows", 200)
    pd.set_option("display.float_format", lambda v: f"{v:+.3f}")
    print(f"\nСквизы, вход против импульса через {ENTRY_LAG_MS} мс, %:")
    print(table.to_string(index=False))


if __name__ == "__main__":
    main()
