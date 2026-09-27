"""Докалибровка стратегии 1 по тиковой истории: отступ входа и выход.

Запускается руками, в `supervisord.conf` не добавляется.

**Это реплей правил по пути цены, а не бэктестер с моделью книги** —
граница та же, что у `orderflow_thresholds.py` (раздел 5 плана
`.ai/docs/test-bots/15-orderflow-plan.md`). Воспроизводится ровно то,
что делает бот `strategy_1` в симуляторе, чтобы подобранное здесь
переносилось в парк без поправок:

* заявки по обе стороны на `entry_offset_atr` × ATR% от цены,
  пересчёт раз в `LEVEL_REFRESH_SECONDS` от текущей цены
  (`wait_for_entry`, `ATR_REFRESH_SECONDS`);
* ATR — Уайлдер по закрытым минутным свечам, в процентах от последнего
  закрытия (`atr_percent` в `candle_store.py`); вне
  `[MIN_ATR_PERCENT; MAX_ATR_PERCENT]` и выше `ATR_SPIKE_GUARD` медиан
  заявок нет;
* исполнение — **пробитие на тик**, по цене заявки; касание не в счёт;
* цель — `take_profit_fraction` отступа, стоп — `stop_loss_atr` × ATR
  от входа, тайм-стоп; стоп раньше цели на одном такте, цель раньше
  таймера (`should_exit`);
* выход по рынку: цена такта минус `EXIT_SLIPPAGE_ATR` × ATR, не меньше
  тика — как `close_price` в `demo_test_bot`;
* после выхода новый круг сразу, от цены выхода.

Цена — последняя сделка такта 100 мс. Бот видит середину спреда, а не
сделку; на отступах от одного ATR разница мала, но на тонкой паре
скачок сделки на bid засчитает пробитие, которого по середине не было.

**Выборка разделена по времени.** Конфигурации ранжируются на
`TRAIN_UNTIL_DAY` первых днях месяца, проверяются на остальных; месяц,
которого при подборе не видели, — ещё одна проверка (`--month`). Иначе
лучшая из сотен конфигураций окажется лучшей по везению — это та же
ловушка, что в `top_bots_report` (раздел 6 плана).

Сторона — только `both`. Разбивка сделок на лонги и шорты внутри неё
печатается, но это приближение к `side=long`/`short`: там бот не
занят противоположной сделкой и сделок было бы больше.

Запуск (внутри контейнера):

    docker exec -it orderflow_general python -m app.scripts.strategy1_calibrate
    docker exec -it orderflow_general python -m app.scripts.strategy1_calibrate --month 2026-09
"""
import argparse
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
    day_grid,
)

# Пары парка strategy_1 (strategy_pairs в рабочей базе). 牛来USDT
# листнули 30 августа — два дня истории, в счёт не берётся.
SYMBOLS = (
    "ACEUSDT", "ALGOUSDT", "BRUSDT", "CUSDT",
    "PIEVERSEUSDT", "SAGAUSDT", "VVVUSDT",
)
KLINES_DIR = "data/futures/um/daily/klines/{symbol}/1m"

# Сетка шире сида (`strategy_1_seed.py`: отступ 1.5/2.5/3.5, цель
# 0.2/0.4/0.6, стоп 1.5/2.5/4, тайм-стоп 15/60/180) в обе стороны по
# отступу — его и калибруем.
ENTRY_OFFSETS_ATR = (1.0, 1.5, 2.0, 2.5, 3.5, 5.0, 7.0)
TAKE_PROFIT_FRACTIONS = (0.2, 0.4, 0.6, 0.8)
STOP_LOSS_ATR = (1.0, 1.5, 2.5, 4.0)
TIME_STOPS_SECONDS = (15, 60, 180)

# Условия эксперимента — как в сиде.
ATR_PERIOD = 14
MIN_ATR_PERCENT = 0.05
MAX_ATR_PERCENT = 10.0
ATR_SPIKE_GUARD = 5.0
EXIT_SLIPPAGE_ATR = 0.1
# Медиана ATR в боте — по ключу в Redis, а там держится MIN_HISTORY = 60
# свечей: ряд уайлдеровского ATR по ним — около 46 значений.
ATR_MEDIAN_WINDOW = 46
LEVEL_REFRESH_SECONDS = 5

# Издержки круга, %: вход maker 0.02, выход taker 0.05. Спред сверх
# этого не начисляется — его заменяет проскальзывание выхода (раздел 7.2
# плана: начислять оба — двойной счёт).
COST_ROUND_TRIP_PERCENT = 0.07

TRAIN_UNTIL_DAY = 20


def minute_atr(paths: list[Path]):
    """ATR% по закрытым свечам, доступный в каждой минуте последних суток.

    Берутся предыдущие сутки и текущие: предыдущие — разгон Уайлдера и
    медианы, чтобы в полночь заявки не пропадали.
    """
    frames = []

    for path in paths:
        with zipfile.ZipFile(path) as archive:
            with archive.open(archive.namelist()[0]) as handle:
                frames.append(pd.read_csv(
                    handle, usecols=["open_time", "high", "low", "close"],
                ))

    candles = pd.concat(frames, ignore_index=True)
    high = candles["high"].to_numpy()
    low = candles["low"].to_numpy()
    close = candles["close"].to_numpy()
    previous = np.roll(close, 1)
    true_range = np.maximum.reduce([
        high - low, np.abs(high - previous), np.abs(low - previous),
    ])
    true_range[0] = np.nan

    atr = np.full(len(close), np.nan)

    if len(close) > ATR_PERIOD:
        atr[ATR_PERIOD] = np.mean(true_range[1:ATR_PERIOD + 1])

        for index in range(ATR_PERIOD + 1, len(close)):
            atr[index] = (
                atr[index - 1] * (ATR_PERIOD - 1) + true_range[index]
            ) / ATR_PERIOD

    median = pd.Series(atr).rolling(
        ATR_MEDIAN_WINDOW, min_periods=ATR_PERIOD,
    ).median().to_numpy()
    percent = atr / close * 100
    usable = (
        (percent >= MIN_ATR_PERCENT) & (percent <= MAX_ATR_PERCENT)
        & ~(atr > median * ATR_SPIKE_GUARD)
    )
    percent = np.where(usable, percent, np.nan)

    # В минуте m доступна свеча m-1: текущая ещё не закрыта.
    day_start = candles["open_time"].iloc[-1] // 86_400_000 * 86_400_000
    today = candles["open_time"].to_numpy() >= day_start
    shifted = np.concatenate([[np.nan], percent[:-1]])

    return shifted[today]


def levels(last, atr_bucket, tick, offset_atr):
    """Уровни на глобальной сетке пересчётов: база — цена в начале блока.

    Зависят только от отступа, поэтому считаются один раз на отступ, а
    не на каждую конфигурацию.
    """
    block = LEVEL_REFRESH_SECONDS * BUCKETS_PER_SECOND
    starts = np.arange(0, BUCKETS_PER_DAY, block)
    base = np.repeat(last[starts], block)[:BUCKETS_PER_DAY]
    base_atr = np.repeat(atr_bucket[starts], block)[:BUCKETS_PER_DAY]
    distance = base * base_atr * offset_atr / 100

    with np.errstate(invalid="ignore"):
        buy_fills = np.flatnonzero(last <= base - distance - tick)
        sell_fills = np.flatnonzero(last >= base + distance + tick)

    return base, base_atr, distance, buy_fills, sell_fills


def replay(last, atr_bucket, tick, config, level_arrays):
    """Один бот на сутки. Сделки: (сторона, валовое %, проскальзывание %)."""
    offset_atr, take_fraction, stop_atr, time_stop = config
    hold = int(time_stop * BUCKETS_PER_SECOND)
    block = LEVEL_REFRESH_SECONDS * BUCKETS_PER_SECOND
    end = BUCKETS_PER_DAY - hold - 1
    base, base_atr, distance, buy_fills, sell_fills = level_arrays

    trades = []
    cursor = 0

    while cursor < end:
        # Остаток текущего блока — от цены на момент старта круга, как
        # сделал бы бот: уровни от устаревшей базы дали бы мгновенный
        # вход сразу после стопа посреди движения.
        block_end = min((cursor // block + 1) * block, end)
        fill = None
        side = 0
        price_now = last[cursor]
        atr_now = atr_bucket[cursor]

        if cursor < block_end and not np.isnan(atr_now):
            reach = price_now * atr_now * offset_atr / 100
            segment = last[cursor + 1:block_end]
            below = np.flatnonzero(segment <= price_now - reach - tick)
            above = np.flatnonzero(segment >= price_now + reach + tick)
            first_below = below[0] if below.size else np.inf
            first_above = above[0] if above.size else np.inf

            if min(first_below, first_above) < np.inf:
                if first_below <= first_above:
                    fill, side = cursor + 1 + int(first_below), 1
                    entry = price_now - reach
                else:
                    fill, side = cursor + 1 + int(first_above), -1
                    entry = price_now + reach
                atr_entry = atr_now

        if fill is None:
            index_buy = np.searchsorted(buy_fills, block_end)
            index_sell = np.searchsorted(sell_fills, block_end)
            next_buy = buy_fills[index_buy] if index_buy < buy_fills.size \
                else BUCKETS_PER_DAY
            next_sell = sell_fills[index_sell] \
                if index_sell < sell_fills.size else BUCKETS_PER_DAY
            fill = min(next_buy, next_sell)

            if fill >= end:
                break

            side = 1 if next_buy <= next_sell else -1
            entry = base[fill] - side * distance[fill]
            atr_entry = base_atr[fill]

        take = entry * atr_entry * offset_atr * take_fraction / 100
        stop = entry * atr_entry * stop_atr / 100
        slip = max(entry * atr_entry * EXIT_SLIPPAGE_ATR / 100, tick)
        path = last[fill + 1:fill + 1 + hold]
        move = (path - entry) * side
        stop_hits = np.flatnonzero(move <= -stop)
        take_hits = np.flatnonzero(move >= take)
        stop_at = stop_hits[0] if stop_hits.size else hold
        take_at = take_hits[0] if take_hits.size else hold

        if stop_at < hold and stop_at <= take_at:
            at = stop_at
        elif take_at < hold:
            at = take_at
        else:
            at = hold - 1

        exit_price = path[at] - side * slip
        trades.append((
            side, (exit_price - entry) * side / entry * 100,
            slip / entry * 100,
        ))
        cursor = fill + 1 + at + 1

    return trades


def process_day(task):
    symbol, path, kline_paths = task
    grid = day_grid(path)

    if grid is None:
        return []

    last = grid[0]
    prices = np.unique(last[~np.isnan(last)])
    tick = float(np.min(np.diff(prices))) if prices.size > 1 else 0.0
    atr_minute = minute_atr(kline_paths)
    atr_bucket = np.repeat(atr_minute, 60 * BUCKETS_PER_SECOND)
    atr_bucket = np.resize(atr_bucket, BUCKETS_PER_DAY)
    atr_bucket[np.isnan(last)] = np.nan

    day = path.name[-14:-4]
    rows = []

    level_arrays = {
        offset: levels(last, atr_bucket, tick, offset)
        for offset in ENTRY_OFFSETS_ATR
    }

    for config in product(
        ENTRY_OFFSETS_ATR, TAKE_PROFIT_FRACTIONS, STOP_LOSS_ATR,
        TIME_STOPS_SECONDS,
    ):
        trades = replay(
            last, atr_bucket, tick, config, level_arrays[config[0]],
        )

        for side in (1, -1):
            gross = np.array([g for s, g, _ in trades if s == side])
            slip = np.array([x for s, _, x in trades if s == side])
            rows.append({
                "symbol": symbol, "day": day,
                "offset": config[0], "take": config[1],
                "stop": config[2], "time_stop": config[3],
                "side": "long" if side > 0 else "short",
                "n": gross.size,
                "gross_sum": gross.sum(),
                "slip_sum": slip.sum(),
                "wins": int((gross > COST_ROUND_TRIP_PERCENT).sum()),
            })

    return rows


def build_tasks(root: Path, month: str):
    tasks = []

    for symbol in SYMBOLS:
        trades_dir = root / AGG_TRADES_DIR.format(symbol=symbol)
        klines_dir = root / KLINES_DIR.format(symbol=symbol)
        klines = sorted(klines_dir.glob(f"{symbol}-1m-*.zip"))
        by_day = {p.name[-14:-4]: p for p in klines}
        days = sorted(by_day)

        for path in sorted(trades_dir.glob(f"{symbol}-aggTrades-{month}-*.zip")):
            day = path.name[-14:-4]

            if day not in by_day:
                continue

            position = days.index(day)
            kline_paths = [by_day[d] for d in days[max(0, position - 1):position + 1]]
            tasks.append((symbol, path, kline_paths))

    return tasks


CONFIG = ["offset", "take", "stop", "time_stop"]


def aggregate(frame: pd.DataFrame) -> pd.DataFrame:
    """Средние по сделкам и t по дням пары (единица — день пары)."""
    grouped = frame.groupby(CONFIG)
    total = grouped[["n", "gross_sum", "slip_sum", "wins"]].sum()
    out = pd.DataFrame({
        "сделок": total["n"],
        "валовое": total["gross_sum"] / total["n"],
        # Сигнал без проскальзывания выхода: сколько даёт сам вход.
        "без проск.": (total["gross_sum"] + total["slip_sum"]) / total["n"],
        "винрейт": total["wins"] / total["n"],
    })
    out["нетто"] = out["валовое"] - COST_ROUND_TRIP_PERCENT

    daily = frame.groupby(CONFIG + ["symbol", "day"])[["n", "gross_sum"]].sum()
    daily = daily[daily["n"] > 0]
    daily["net"] = daily["gross_sum"] / daily["n"] - COST_ROUND_TRIP_PERCENT
    stats = daily.groupby(CONFIG)["net"].agg(["mean", "std", "count"])
    out["t нетто (дни)"] = stats["mean"] / stats["std"] * np.sqrt(stats["count"])

    per_pair = frame.groupby(CONFIG + ["symbol"])[["n", "gross_sum"]].sum()
    per_pair = per_pair["gross_sum"] / per_pair["n"] - COST_ROUND_TRIP_PERCENT
    out["пар нетто>0"] = (per_pair > 0).groupby(CONFIG).sum()

    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--month", default="2026-08")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args()

    root = Path(os.environ.get("BINANCE_HISTORY_DIR", DEFAULT_HISTORY_DIR))
    tasks = build_tasks(root, args.month)
    print(f"{args.month}: пар {len(SYMBOLS)}, суток {len(tasks)}", flush=True)
    rows = []

    with ProcessPoolExecutor(args.workers) as pool:
        for done, day_rows in enumerate(pool.map(process_day, tasks), 1):
            rows.extend(day_rows)

            if done % 20 == 0:
                print(f"  {done}/{len(tasks)}", flush=True)

    frame = pd.DataFrame(rows)
    out = root / "analysis" / f"strategy1_calibrate_{args.month}"
    frame.to_csv(f"{out}.csv.gz", index=False)

    pd.set_option("display.width", 250)
    pd.set_option("display.max_columns", 30)
    pd.set_option("display.float_format", lambda v: f"{v:+.4f}")

    day_number = frame["day"].str[-2:].astype(int)
    train = aggregate(frame[day_number <= TRAIN_UNTIL_DAY])
    test = aggregate(frame[day_number > TRAIN_UNTIL_DAY])
    whole = aggregate(frame)

    print("\nПо отступу входа, лучшая конфигурация на обучении и она же на проверке:")
    best = train.reset_index().sort_values("нетто", ascending=False)
    best = best.groupby("offset").head(1).set_index(CONFIG)
    joined = best[["сделок", "валовое", "без проск.", "нетто"]].join(
        test[["сделок", "валовое", "без проск.", "нетто", "t нетто (дни)",
              "пар нетто>0"]],
        rsuffix=" проверка",
    )
    print(joined.sort_index().to_string())

    print(f"\nТоп-{args.top} на обучении (1–{TRAIN_UNTIL_DAY}) → проверка:")
    top = train.sort_values("нетто", ascending=False).head(args.top)
    print(top.join(test, rsuffix=" проверка").to_string())

    print("\nСетка сида (strategy_1_seed.py) на всём месяце, по отступу:")
    seed = whole.reset_index()
    seed = seed[
        seed["offset"].isin([1.5, 2.5, 3.5]) & seed["take"].isin([0.2, 0.4, 0.6])
        & seed["stop"].isin([1.5, 2.5, 4.0])
    ]
    print(seed.groupby("offset")[
        ["валовое", "без проск.", "нетто"]
    ].mean().to_string())

    print("\nДоля конфигураций с валовым > 0 и с нетто > 0, весь месяц:")
    print(f"  валовое > 0: {(whole['валовое'] > 0).mean():.1%}, "
          f"нетто > 0: {(whole['нетто'] > 0).mean():.1%} "
          f"из {len(whole)}")

    print(f"  без проскальзывания > 0: {(whole['без проск.'] > 0).mean():.1%}")

    sides = frame.groupby("side")[["n", "gross_sum"]].sum()
    print("\nЛонги и шорты внутри side=both, валовое на сделку по всем конфигурациям:")
    print((sides["gross_sum"] / sides["n"]).to_string())


if __name__ == "__main__":
    main()
