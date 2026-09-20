"""Профиль пар: чем каждая живёт с точки зрения стратегии прострелов.

Отчёт ничего не отбирает и никуда не пишет — он измеряет и печатает.
Это намеренно: пороги вроде «ATR в верхних 70% процентиля» — гипотезы о
том, что предсказывает прибыльность, а проверяются они сделками. Жёсткий
фильтр отрезал бы пары раньше, чем мы узнали, правильно ли он режет.

История тянется по REST в момент запуска (`candle_history.fetch_history`),
а не берётся из Redis: там лежит последний час, и процентилю ATR сравнивать
не с чем.

    # все пары watched_pair за трое суток
    python -m app.scripts.pair_profile_report

    # свой список, неделя, порог прострела 0.3%
    python -m app.scripts.pair_profile_report --symbols BTCUSDT,BMTUSDT \\
        --hours 168 --spike-percent 0.3

Колонки:

* `ATR%` — ATR(14) в процентах от цены, то есть обычный ход за минуту;
* `проц.` — в каком процентиле собственной истории сейчас этот ATR:
  100 означает, что пара разогналась сильнее, чем когда-либо за окно;
* `тени>тела` — в скольких процентах минут тени длиннее тела, то есть
  цену выносило и возвращало обратно. Это и есть профиль прострелов;
* `прострелов/сут` — сколько свечей в сутки с тенью не короче порога;
* `порог%` — сам порог в процентах: при `--spike-atr` он у каждой пары
  свой, потому что считается от её же ATR;
* `оборот/сут` — оценка суточного оборота в USDT (объём × цена).
"""
import argparse
import asyncio
from decimal import Decimal
from statistics import median

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import AssetExchangeSpec, WatchedPair
from app.sub_services.watchers.candle_history import fetch_many
from app.sub_services.watchers.candle_store import (
    atr,
    atr_percent,
    atr_series,
    percentile_of,
    shadow_share,
    spike_count,
)

DEFAULT_HOURS = 72
DEFAULT_SPIKE_PERCENT = Decimal("0.5")
DEFAULT_ATR_PERIOD = 14

SORT_KEYS = {
    "spikes": "прострелов в сутки",
    "atr": "ATR в процентах",
    "percentile": "процентиль ATR",
    "shadow": "доля свечей с тенями длиннее тела",
    "volume": "оборот",
    "symbol": "имя пары",
}


async def watched_symbols() -> list[str]:
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        stmt = select(AssetExchangeSpec.symbol).join(WatchedPair.asset_exchange)
        result = await session.execute(stmt)

        return sorted({row[0] for row in result.fetchall() if row[0]})


def profile(symbol: str, candles: list[dict], atr_period: int,
            spike_percent: Decimal, spike_atr: Decimal | None) -> dict:
    series = atr_series(candles, atr_period)
    current = atr(candles, atr_period)

    if spike_atr is not None:
        # Порог в процентах на разных парах означает разное: у пары с
        # ATR 1.3% тень в 0.3% — обычная минута, у спокойной — событие.
        # В единицах ATR порог сравним между парами.
        typical = median(series) if series else None
        last_close = Decimal(candles[-1]["c"]) if candles else None

        if typical and last_close:
            spike_percent = typical / last_close * Decimal(100) * spike_atr
    days = Decimal(len(candles)) / Decimal(1440) or Decimal(1)

    # Оборот считаем как объём × цена закрытия: quote-объёма в свече мы не
    # храним, а для сравнения пар между собой этой точности достаточно.
    turnover = Decimal(0)
    for candle in candles[-1440:]:
        try:
            turnover += Decimal(candle.get("v", "0")) * Decimal(candle["c"])
        except Exception:
            continue

    return {
        "symbol": symbol,
        "threshold": spike_percent,
        "candles": len(candles),
        "hours": Decimal(len(candles)) / Decimal(60),
        "atr": atr_percent(candles, atr_period),
        "percentile": percentile_of(series, current) if current is not None else None,
        "shadow": shadow_share(candles),
        "spikes": Decimal(spike_count(candles, spike_percent)) / days,
        "volume": turnover,
    }


def render(rows: list[dict], sort_key: str, top: int, threshold: str) -> str:
    def key(row):
        value = row.get(sort_key)

        if sort_key == "symbol":
            return row["symbol"]

        return value if value is not None else Decimal(-1)

    rows = sorted(rows, key=key, reverse=sort_key != "symbol")[:top]

    header = (f"{'пара':<14}{'ATR%':>8}{'проц.':>8}{'тени>тела%':>12}"
              f"{'прострелов/сут':>16}{'порог%':>9}{'оборот/сут':>16}")
    lines = [header, "-" * len(header)]

    for row in rows:
        def number(value, digits=2):
            return "—" if value is None else f"{value:.{digits}f}"

        volume = row["volume"]
        volume_text = "—" if volume is None else f"{volume / 1000:,.0f}k".replace(",", " ")

        lines.append(
            f"{row['symbol']:<14}"
            f"{number(row['atr'], 3):>8}"
            f"{number(row['percentile'], 0):>8}"
            f"{number(row['shadow'], 1):>12}"
            f"{number(row['spikes'], 1):>16}"
            f"{number(row['threshold'], 3):>9}"
            f"{volume_text:>16}"
        )

    lines.append("")
    lines.append(f"Прострел — свеча с тенью не короче {threshold}.")
    lines.append(f"Сортировка: {SORT_KEYS[sort_key]}. Пар в отчёте: {len(rows)}.")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Профиль пар для стратегии прострелов")
    parser.add_argument("--symbols", help="список через запятую; по умолчанию все watched_pair")
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS, help="глубина истории")
    parser.add_argument("--spike-percent", type=Decimal, default=DEFAULT_SPIKE_PERCENT,
                        help="порог прострела в процентах от цены")
    parser.add_argument("--spike-atr", type=Decimal,
                        help="порог прострела в ATR пары; заменяет --spike-percent")
    parser.add_argument("--atr-period", type=int, default=DEFAULT_ATR_PERIOD)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--sort", choices=sorted(SORT_KEYS), default="spikes")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--market", choices=("futures", "spot"), default="futures",
                        help="рынок истории; по умолчанию фьючерсы — на них торгует симулятор")
    args = parser.parse_args()

    if args.symbols:
        symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    else:
        symbols = await watched_symbols()

    if not symbols:
        print("Список пар пуст: заполните watched_pair или задайте --symbols.")
        return

    print(f"Пар: {len(symbols)}, глубина {args.hours:.0f} ч, рынок {args.market}. "
          f"Тяну историю…")

    async with httpx.AsyncClient(timeout=30) as client:
        history = await fetch_many(client, symbols, args.hours, args.concurrency, args.market)

    if not history:
        print("Ни по одной паре история не получена.")
        return

    skipped = [symbol for symbol in symbols if symbol not in history]

    rows = [profile(symbol, candles, args.atr_period, args.spike_percent, args.spike_atr)
            for symbol, candles in history.items()]

    threshold = (f"{args.spike_atr} ATR пары" if args.spike_atr is not None
                 else f"{args.spike_percent}% от цены")

    print()
    print(render(rows, args.sort, args.top, threshold))

    if skipped:
        print(f"Без истории на рынке {args.market}: {', '.join(sorted(skipped))}")


if __name__ == "__main__":
    asyncio.run(main())
