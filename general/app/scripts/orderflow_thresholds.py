"""Офлайн-счёт порогов прострела по тиковой истории (раздел 5.1 плана).

Запускается руками, в `supervisord.conf` не добавляется.

**Это не бэктестер, и им быть не должен.** Модели матчинга, очереди в
книге и частичных исполнений здесь нет намеренно: такая модель стоит
месяцев и врёт ровно там, где интересно. Считаются распределения —
сколько цена проходит после срабатывания триггера, — а проверяет числа
уже симулятор на живых ценах, где модель исполнения общая со всем
парком и сравнение честное.

Что считается для каждой пары и каждой пары «окно × порог движения»:

* **условное распределение движения вперёд** на горизонтах
  `HORIZONS_SECONDS` — медиана и квантили, а не среднее: у прострелов
  хвост в одну сторону, и среднее покажет прибыль там, где восемь
  сделок из десяти убыточные;
* **MFE и MAE** — максимальный ход в свою и в чужую сторону за горизонт.
  Из них читаются `take_profit_percent` и `stop_loss_percent`: это
  вопрос о пути цены, а не об исполнении, и модели матчинга он не
  требует;
* **дельта** по `is_buyer_maker` в окне перед триггером — чтобы потом
  условить пороги на неё.

Триггер повторяет `wait_for_entry` из
`app/strategies/strategy_0/algorithm.py`: скользящее окно
`window_seconds`, сравнение самой старой цены в окне с текущей, вход при
`|change| >= move_percent`. Сетка берётся оттуда же — `WINDOW_SECONDS` и
`MOVE_PERCENTS` из `app/constants/strategy_0_seed.py`, чтобы считать
ровно то, что будет исполнять парк.

**Три поправки на реальность, каждая из которых меняет ответ.**

1. **Отсчёт не от цены триггера, а от цены через ENTRY_LAG_MS.** Между
   решением и заявкой проходит 100-200 мс, и на прострелах это и есть
   разница между прибылью и тем, что уже разобрали без нас. Для
   сравнения считается и вариант без задержки — колонка `lag_cost`
   показывает, сколько стоит эта задержка.
2. **Издержки по своим ставкам.** Комиссия `COMMISSION_OPEN` +
   `COMMISSION_CLOSE` = 0.1% за круг (на USDⓈ-M тейкер вдвое дешевле
   спотового) плюс замеренный на парке спред `SPREAD_ROUND_TRIP_PERCENT`
   = 0.0264%. Барьер выходит ≈0.13% за круг, а не ≈0.23%, которые
   получатся со спотовой ставкой, — и нижние пороги движения из-за этого
   рабочие, а не отбрасываются заранее.
3. **События не перекрываются.** После срабатывания следующий триггер
   берётся не раньше чем через `EVENT_COOLDOWN_SECONDS`: бот в это время
   держит позицию и второй раз войти не может, а перекрывающиеся события
   делают выборку зависимой и раздувают значимость.

**Чего этот счёт не знает.** `aggTrades` — цены сделок, а бот видит
середину спреда из Redis. Разница до половины спреда; она учтена как
издержка, но не как шум цены. Стакана в дампах нет вовсе, поэтому
порогов по OBI здесь не будет — они считаются по своему сбору
(`order_book_listener.py`, раздел 5.2).

Запуск (внутри контейнера — там numpy и pandas, и туда смонтирован
архив):

    docker exec -it orderflow_general python -m app.scripts.orderflow_thresholds --symbols ALGOUSDT
    docker exec -it orderflow_general python -m app.scripts.orderflow_thresholds
"""
import argparse
import os
import sys
import zipfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from app.constants.commissions import COMMISSION_OPEN, COMMISSION_CLOSE
from app.constants.strategy_0_seed import MOVE_PERCENTS, WINDOW_SECONDS
from app.strategies.strategy_0.algorithm import (
    ENTRY_CONTINUATION,
    ENTRY_REVERSAL,
    POLL_SECONDS,
)

# Шаг сетки — такт опроса цены у стратегии. Считать мельче, чем бот
# видит, значит мерить то, чего он не заметит.
GRID_MS = int(POLL_SECONDS * 1000)
BUCKETS_PER_SECOND = 1000 // GRID_MS
BUCKETS_PER_DAY = 24 * 60 * 60 * BUCKETS_PER_SECOND

# Задержка от решения до заявки. 150 мс — середина вилки 100-200 мс из
# раздела 5.1; округляется вверх до целого такта сетки.
ENTRY_LAG_MS = 150
ENTRY_LAG_BUCKETS = -(-ENTRY_LAG_MS // GRID_MS)

# Горизонты удержания. Первые три — окрестность `MAX_HOLD_SECONDS`
# сетки (30/60/180), остальные короче: прострел, который не окупился за
# пять секунд, скорее всего уже разобран.
HORIZONS_SECONDS = (1, 2, 5, 10, 30, 60, 180)

# Столько бот держит позицию максимум, и столько же события не должны
# перекрываться.
EVENT_COOLDOWN_SECONDS = max(HORIZONS_SECONDS)

# Спред за круг по умолчанию — замер на своём парке (раздел 7.2 плана:
# 0.264 на номинал 1000). Годится только как запасной вариант: замер
# 2026-09-25 по 570 парам дал медиану 0.0382%, а разброс от 0.0001% до
# 0.132%, то есть у двух третей пар барьер выше этой константы.
#
# Если рядом лежит таблица спредов по парам (её пишет разбор архива
# стакана, см. SPREADS_FILE), барьер считается для каждой пары свой.
# Иначе пороги на широкой паре оказываются завышены, а на узкой
# занижены, и обе ошибки смотрят в одну сторону — в оптимизм.
SPREAD_ROUND_TRIP_PERCENT = 0.0264
SPREADS_FILE = "analysis/pair_spreads.csv"
COMMISSION_ROUND_TRIP_PERCENT = float(
    (COMMISSION_OPEN + COMMISSION_CLOSE) * 100
)
COST_ROUND_TRIP_PERCENT = (
    COMMISSION_ROUND_TRIP_PERCENT + SPREAD_ROUND_TRIP_PERCENT
)

# По каким движениям выгружать события поштучно. На крупных движениях
# событий на пару — единицы, и статистику по ним можно строить только
# объединив пары: медиана по одиннадцати наблюдениям — это шум, а не
# характер пары. Мелкие движения дают сотни тысяч событий, там хватает
# сводки.
DUMP_MOVE_PERCENTS_FROM = 1.0

DEFAULT_HISTORY_DIR = "/data/binance-history"
AGG_TRADES_GLOB = "data/futures/um/daily/aggTrades/{symbol}"

CSV_COLUMNS = ["price", "quantity", "transact_time", "is_buyer_maker"]


def day_grid(frame: pd.DataFrame, day_start_ms: int):
    """Сетка `GRID_MS` за сутки: цена, объём покупок и продаж.

    Цена в пустом такте — последняя известная (forward-fill): бот в
    такте без сделок видит ровно её, а не пропуск. Такты до первой
    сделки суток остаются NaN и в триггеры не попадают.
    """
    index = ((frame["transact_time"].to_numpy() - day_start_ms) // GRID_MS)
    index = index.astype(np.int64)
    keep = (index >= 0) & (index < BUCKETS_PER_DAY)
    index = index[keep]

    price = frame["price"].to_numpy(dtype=np.float64)[keep]
    qty = frame["quantity"].to_numpy(dtype=np.float64)[keep]
    # is_buyer_maker=true — агрессором был продавец.
    sell = frame["is_buyer_maker"].to_numpy()[keep]

    grid_price = np.full(BUCKETS_PER_DAY, np.nan)
    # Последняя сделка такта: пишем по порядку, последняя запись победит.
    grid_price[index] = price

    buys = np.zeros(BUCKETS_PER_DAY)
    sells = np.zeros(BUCKETS_PER_DAY)
    np.add.at(buys, index[~sell], qty[~sell])
    np.add.at(sells, index[sell], qty[sell])

    # Forward-fill без питоновского цикла.
    known = ~np.isnan(grid_price)

    if not known.any():
        return None

    positions = np.where(known, np.arange(BUCKETS_PER_DAY), 0)
    np.maximum.accumulate(positions, out=positions)
    filled = grid_price[positions]
    filled[:np.argmax(known)] = np.nan

    return filled, buys, sells


def rising_edges(fired: np.ndarray, cooldown: int) -> np.ndarray:
    """Начала срабатываний, разнесённые не ближе чем на `cooldown`.

    Подряд идущие такты одного и того же движения — это одно событие, а
    не сотня: бот входит один раз и дальше держит позицию.
    """
    candidates = np.flatnonzero(fired)

    if candidates.size == 0:
        return candidates

    kept = [candidates[0]]

    for position in candidates[1:]:
        if position - kept[-1] >= cooldown:
            kept.append(position)

    return np.array(kept, dtype=np.int64)


def measure(prices, buys, sells, window_seconds, move_percent):
    """События одного правила и всё, что по ним меряется."""
    lag = ENTRY_LAG_BUCKETS
    span = int(window_seconds * BUCKETS_PER_SECOND)
    horizon_buckets = [h * BUCKETS_PER_SECOND for h in HORIZONS_SECONDS]
    longest = max(horizon_buckets)

    if prices.size <= span + lag + longest:
        return []

    old = prices[:-span]
    new = prices[span:]
    change = np.full(prices.size, np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        change[span:] = (new - old) / old * 100

    fired = np.abs(change) >= float(move_percent)
    fired &= np.isfinite(change)
    # Событию нужно будущее: и задержка входа, и самый длинный горизонт.
    fired[-(lag + longest):] = False

    events = rising_edges(fired, EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND)

    if events.size == 0:
        return []

    direction = np.sign(change[events])
    entry = prices[events + lag]
    trigger_price = prices[events]

    # Сколько съела задержка: движение, ушедшее между решением и заявкой.
    lag_cost = (entry - trigger_price) / trigger_price * 100 * direction

    # Дельта в окне перед триггером — накопленным суммированием.
    buy_sum = np.concatenate(([0.0], np.cumsum(buys)))
    sell_sum = np.concatenate(([0.0], np.cumsum(sells)))
    window_buy = buy_sum[events + 1] - buy_sum[np.maximum(events - span, 0)]
    window_sell = sell_sum[events + 1] - sell_sum[np.maximum(events - span, 0)]
    volume = window_buy + window_sell
    with np.errstate(invalid="ignore", divide="ignore"):
        delta = np.where(volume > 0, (window_buy - window_sell) / volume, np.nan)

    rows = []

    for mode in (ENTRY_CONTINUATION, ENTRY_REVERSAL):
        side = direction if mode == ENTRY_CONTINUATION else -direction

        for seconds, buckets in zip(HORIZONS_SECONDS, horizon_buckets):
            exit_price = prices[events + lag + buckets]
            gross = (exit_price - entry) / entry * 100 * side

            # MFE и MAE: крайние точки пути внутри горизонта. Считаются
            # по той же сетке, что видит бот.
            window_slice = np.lib.stride_tricks.sliding_window_view(
                prices, buckets + 1
            )[events + lag]
            best = window_slice.max(axis=1)
            worst = window_slice.min(axis=1)
            up = (best - entry) / entry * 100
            down = (worst - entry) / entry * 100
            mfe = np.where(side > 0, up, -down)
            mae = np.where(side > 0, -down, up)

            rows.append({
                "window_seconds": window_seconds,
                # Знак самого движения, а не стороны входа. Без него
                # нельзя отличить «гейт по дельте работает» от «выброс
                # вверх и выброс вниз ведут себя по-разному»: дельта в
                # окне почти всегда совпадает по знаку с движением,
                # которое она же и создала.
                "move_direction": direction,
                "move_percent": float(move_percent),
                "entry_mode": mode,
                "horizon_seconds": seconds,
                "events": int(events.size),
                "gross": gross,
                "mfe": mfe,
                "mae": mae,
                "delta": delta,
                "lag_cost": lag_cost,
                "agrees": np.sign(delta) == side,
            })

    return rows


def read_day(path: Path) -> pd.DataFrame | None:
    try:
        with zipfile.ZipFile(path) as archive:
            name = archive.namelist()[0]
            with archive.open(name) as handle:
                frame = pd.read_csv(
                    handle, usecols=CSV_COLUMNS,
                    dtype={
                        "price": np.float64, "quantity": np.float64,
                        "transact_time": np.int64, "is_buyer_maker": bool,
                    },
                )
    except Exception as error:
        print(f"  пропускаю {path.name}: {error}", flush=True)
        return None

    return frame if len(frame) else None


def day_start_ms(path: Path) -> int:
    day = path.stem.rsplit("-", 3)[-3:]

    return int(pd.Timestamp("-".join(day), tz="UTC").value // 1_000_000)


def load_pair_spreads(root: Path) -> dict[str, float]:
    """Спред по каждой паре из разбора архива стакана.

    Пусто — если таблицы нет: тогда у всех пар общий барьер, и об этом
    печатается предупреждение. Молча считать по константе нельзя, это
    ровно та ошибка, ради которой таблица и заведена.
    """
    path = root / SPREADS_FILE

    if not path.exists():
        return {}

    frame = pd.read_csv(path)

    return dict(zip(frame["symbol"], frame["spread_percent"]))


def analyse_symbol(arguments) -> tuple[str, list[dict], list]:
    symbol, root, spread = arguments
    directory = Path(root) / AGG_TRADES_GLOB.format(symbol=symbol)
    days = sorted(directory.glob("*.zip")) if directory.exists() else []

    if not days:
        return symbol, [], []

    cost = COMMISSION_ROUND_TRIP_PERCENT + spread
    collected: dict[tuple, dict] = {}

    for path in days:
        frame = read_day(path)

        if frame is None:
            continue

        built = day_grid(frame, day_start_ms(path))

        if built is None:
            continue

        prices, buys, sells = built

        for window in WINDOW_SECONDS:
            for move in MOVE_PERCENTS:
                for row in measure(prices, buys, sells, window, move):
                    key = (
                        row["window_seconds"], row["move_percent"],
                        row["entry_mode"], row["horizon_seconds"],
                    )
                    bucket = collected.setdefault(
                        key, {"gross": [], "mfe": [], "mae": [], "delta": [],
                              "lag_cost": [], "agrees": [], "move_direction": []}
                    )
                    for field in bucket:
                        bucket[field].append(row[field])

    summary = []
    events: list[pd.DataFrame] = []

    for key, bucket in sorted(collected.items()):
        gross = np.concatenate(bucket["gross"])
        finite = np.isfinite(gross)
        gross = gross[finite]

        if gross.size == 0:
            continue

        mfe = np.concatenate(bucket["mfe"])[finite]
        mae = np.concatenate(bucket["mae"])[finite]
        delta = np.concatenate(bucket["delta"])[finite]
        lag_cost = np.concatenate(bucket["lag_cost"])[finite]
        move_direction = np.concatenate(bucket["move_direction"])[finite]
        agrees = np.concatenate(bucket["agrees"])[finite]
        net = gross - cost

        window, move, mode, horizon = key
        row = {
            "symbol": symbol,
            "cost_percent": cost,
            "window_seconds": window,
            "move_percent": move,
            "entry_mode": mode,
            "horizon_seconds": horizon,
            "events": int(gross.size),
            "net_p10": float(np.percentile(net, 10)),
            "net_p25": float(np.percentile(net, 25)),
            "net_median": float(np.median(net)),
            # Среднее рядом с медианой не для красоты: у прострелов
            # хвост в одну сторону, и правило с медианой +0.5% и
            # средним −2% разоряет. Решение принимается по обоим.
            "net_mean": float(np.mean(net)),
            "net_p75": float(np.percentile(net, 75)),
            "net_p90": float(np.percentile(net, 90)),
            "win_rate": float((net > 0).mean()),
            "mfe_median": float(np.median(mfe)),
            "mfe_p75": float(np.percentile(mfe, 75)),
            "mae_median": float(np.median(mae)),
            "mae_p75": float(np.percentile(mae, 75)),
            "lag_cost_median": float(np.median(lag_cost)),
            "delta_median": float(np.nanmedian(delta)) if delta.size else float("nan"),
        }

        # Та же медиана, но только на событиях, где дельта смотрит в
        # сторону входа. Это первая проверка гипотезы «гейт по дельте
        # помогает» — ещё до того, как гейт написан.
        if agrees.any():
            row["net_median_delta_agrees"] = float(np.median(net[agrees]))
            row["events_delta_agrees"] = int(agrees.sum())
        else:
            row["net_median_delta_agrees"] = float("nan")
            row["events_delta_agrees"] = 0

        summary.append(row)

        if move >= DUMP_MOVE_PERCENTS_FROM:
            events.append(pd.DataFrame({
                "symbol": symbol,
                "window_seconds": window,
                "move_percent": move,
                "entry_mode": mode,
                "horizon_seconds": horizon,
                "net": net,
                "mfe": mfe,
                "mae": mae,
                "delta": delta,
                "move_direction": move_direction,
            }))

    return symbol, summary, events


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Пороги прострела по истории aggTrades"
    )
    parser.add_argument("--symbols", help="список через запятую")
    parser.add_argument("--jobs", type=int, default=6)
    parser.add_argument("--out", help="куда положить CSV")
    arguments = parser.parse_args()

    root = Path(
        os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR)
    ).expanduser()
    base = root / "data/futures/um/daily/aggTrades"

    if arguments.symbols:
        symbols = [s.strip() for s in arguments.symbols.split(",") if s.strip()]
    else:
        symbols = sorted(p.name for p in base.iterdir() if p.is_dir())

    if not symbols:
        print(f"В {base} нет ни одной пары. Сначала fetch_binance_history.")
        return 1

    out = Path(
        arguments.out or (root / "analysis" / "strategy_0_thresholds.csv")
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    print(
        f"Пар: {len(symbols)}. Сетка: окна {WINDOW_SECONDS}, движения "
        f"{[str(m) for m in MOVE_PERCENTS]}%, горизонты "
        f"{HORIZONS_SECONDS} с.\n"
        f"Барьер издержек за круг: {COST_ROUND_TRIP_PERCENT:.4f}% "
        f"(комиссия {COMMISSION_ROUND_TRIP_PERCENT:.2f} + спред "
        f"{SPREAD_ROUND_TRIP_PERCENT}).\n"
        f"Задержка входа: {ENTRY_LAG_MS} мс "
        f"({ENTRY_LAG_BUCKETS} тактов по {GRID_MS} мс).",
        flush=True,
    )

    spreads = load_pair_spreads(root)

    if spreads:
        covered = sum(1 for symbol in symbols if symbol in spreads)
        print(
            f"Спред по парам: таблица на {len(spreads)} пар, покрывает "
            f"{covered} из {len(symbols)}. Остальным — {SPREAD_ROUND_TRIP_PERCENT}%.",
            flush=True,
        )
    else:
        print(
            f"⚠️  Таблицы спредов нет ({root / SPREADS_FILE}), у всех пар "
            f"общий барьер {COST_ROUND_TRIP_PERCENT:.4f}%. Пороги на широких "
            f"парах выйдут завышенными.",
            flush=True,
        )

    rows = []
    dumped = []
    tasks = [
        (symbol, str(root), spreads.get(symbol, SPREAD_ROUND_TRIP_PERCENT))
        for symbol in symbols
    ]

    with ProcessPoolExecutor(max_workers=arguments.jobs) as pool:
        for symbol, summary, events in pool.map(analyse_symbol, tasks):
            rows.extend(summary)
            dumped.extend(events)
            print(f"  {symbol:<18} строк {len(summary)}", flush=True)

    if not rows:
        print("Ни одного события — считать нечего.")
        return 1

    frame = pd.DataFrame(rows)
    frame.to_csv(out, index=False)
    print(f"\nГотово: {len(frame)} строк → {out}")

    if dumped:
        events_path = out.with_name(out.stem + "_events.csv.gz")
        pd.concat(dumped, ignore_index=True).to_csv(
            events_path, index=False, compression="gzip"
        )
        print(f"События поштучно (движение от {DUMP_MOVE_PERCENTS_FROM}%): "
              f"{events_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
