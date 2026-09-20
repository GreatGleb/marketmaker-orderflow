"""Скрининг всего фьючерсного пула под стратегию 1.

Отличие от `pair_profile_report`: тот измеряет заданный список, этот
выбирает из вселенной. Отбор двухступенчатый, потому что тянуть глубокую
историю по девятистам парам — это десятки тысяч единиц веса запросов и
полчаса ожидания ради пар, которые отсеются на первом же пороге.

1. Дешёвый проход: `ticker/24hr` (один запрос на всю биржу) отсекает по
   обороту, `ticker/bookTicker` (тоже один) — по спреду. Сухие и
   неликвидные уходят здесь.
2. Короткая история (`--fast-hours`) по выжившим: частота выносов,
   возврат, плотность сделок.
3. Глубокая история (`--hours`) по лучшим из второго шага — на ней и
   считается итоговый рейтинг.

Метрики выбраны под механику стратегии: заявка ловит вынос, позиция
живёт секунды, выход рыночный.

    docker exec -it orderflow_general python -m app.scripts.pair_screener
    docker exec -it orderflow_general python -m app.scripts.pair_screener \\
        --min-turnover 50000000 --top 12
"""
import argparse
import asyncio
from decimal import Decimal, InvalidOperation

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import AssetExchangeSpec
from app.sub_services.watchers.candle_history import fetch_history
from app.sub_services.watchers.candle_store import (
    atr_percent,
    atr_series,
    percentile_of,
    rebound_share,
    shadow_share,
    trade_density,
)

TICKER_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
BOOK_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

# Порог оборота: ниже него рыночный выход двигает цену сам.
DEFAULT_MIN_TURNOVER = Decimal("20000000")
# Спред шире этого съедает цель: она составляет доли ATR.
DEFAULT_MAX_SPREAD_PERCENT = Decimal("0.05")
# Минимум сделок в минуту: на паре тише заявка простоит весь тайм-стоп.
DEFAULT_MIN_TRADES = 30
# Доля пустых минут, выше которой пара не годится.
DEFAULT_MAX_DRY_PERCENT = Decimal("2")
# Выносом считается тень от двух ATR, возвратом — 30% её длины за две
# следующие минуты: тайм-стоп стратегии измеряется секундами.
SQUEEZE_ATR_MULTIPLE = Decimal(2)
REBOUND_FRACTION = Decimal("0.3")
REBOUND_HORIZON = 2

ATR_PERIOD = 14

# Параметры стратегии, от которых считается ожидаемая прибыль сделки.
# Держатся здесь, а не импортируются из сетки парка: там они перебираются,
# а отбор пары должен опираться на одно, срединное правило.
STRATEGY_OFFSET_ATR = Decimal("2.5")
STRATEGY_TAKE_FRACTION = Decimal("0.4")
STRATEGY_SLIPPAGE_ATR = Decimal("0.1")

# Во сколько раз цель сделки должна превышать её издержки. Главная
# отсечка отбора: на паре, где цель сравнима с комиссией, стратегия
# убыточна при любой доле возвратов.
DEFAULT_MIN_EDGE = Decimal(3)

# Ставки на случай, если пара не засеяна: стандартные для фьючерсов.
FALLBACK_MAKER = Decimal("0.0002")
FALLBACK_TAKER = Decimal("0.0005")
# Запас по весу запросов: лимит fapi — 2400 в минуту, страница свечей
# стоит 5. Пять одновременных запросов оставляют место всему остальному.
CONCURRENCY = 5


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None

    return number if number.is_finite() else None


async def universe(client, min_turnover: Decimal, max_spread: Decimal) -> list[dict]:
    """Пары биржи, прошедшие дешёвые пороги: оборот и спред."""
    tickers = (await client.get(TICKER_URL)).json()
    books = {row["symbol"]: row for row in (await client.get(BOOK_URL)).json()}

    rows = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if not (symbol.endswith("USDT") or symbol.endswith("USDC")):
            continue

        turnover = _decimal(ticker.get("quoteVolume"))

        if turnover is None or turnover < min_turnover:
            continue

        book = books.get(symbol)
        bid = _decimal(book.get("bidPrice")) if book else None
        ask = _decimal(book.get("askPrice")) if book else None

        if not bid or not ask or ask <= bid:
            continue

        spread = (ask - bid) / ((ask + bid) / 2) * 100

        if spread > max_spread:
            continue

        rows.append({"symbol": symbol, "turnover": turnover, "spread": spread})

    return rows


async def commission_rates() -> dict:
    """Ставки maker/taker по парам из `asset_exchange_specs`."""
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        rows = (await session.execute(select(
            AssetExchangeSpec.symbol,
            AssetExchangeSpec.maker_commission_rate,
            AssetExchangeSpec.taker_commission_rate,
        ))).fetchall()

    return {
        symbol: (
            maker if maker is not None else FALLBACK_MAKER,
            taker if taker is not None else FALLBACK_TAKER,
        )
        for symbol, maker, taker in rows
    }


def edge_ratio(atr: Decimal | None, spread: Decimal,
               rates: tuple[Decimal, Decimal]) -> Decimal | None:
    """Во сколько раз цель сделки превышает её издержки.

    Цель стратегии — доля отступа, то есть доля ATR. Издержки — комиссия
    входа (maker) и выхода (taker), спред и проскальзывание выхода. Если
    отношение около единицы, пара проигрышна независимо от того, как
    часто на ней случаются выносы и как охотно они возвращаются: каждая
    сделка отдаёт бирже больше, чем приносит рынок.
    """
    if not atr:
        return None

    maker, taker = rates
    target = atr * STRATEGY_OFFSET_ATR * STRATEGY_TAKE_FRACTION
    costs = (
        (maker + taker) * 100
        + spread
        + atr * STRATEGY_SLIPPAGE_ATR
    )

    return target / costs if costs else None


def measure(symbol: str, candles: list[dict], spread: Decimal,
            turnover: Decimal, rates: tuple[Decimal, Decimal]) -> dict:
    """Метрики пары под механику стратегии."""
    events, rebound = rebound_share(
        candles, ATR_PERIOD, SQUEEZE_ATR_MULTIPLE, REBOUND_FRACTION,
        REBOUND_HORIZON,
    )
    trades, dry = trade_density(candles)
    series = atr_series(candles, ATR_PERIOD)
    current = series[-1] if series else None
    days = Decimal(len(candles)) / Decimal(1440) or Decimal(1)
    atr = atr_percent(candles, ATR_PERIOD)

    return {
        "symbol": symbol,
        "candles": len(candles),
        "atr": atr,
        "percentile": percentile_of(series, current) if current else None,
        "shadow": shadow_share(candles),
        "squeezes": Decimal(events) / days,
        "events": events,
        "rebound": rebound,
        "trades": trades,
        "dry": dry,
        "spread": spread,
        "turnover": turnover,
        # Спред в долях ATR: он вычитается из каждой сделки, а цель
        # стратегии — доля ATR, поэтому сравнивать надо с ним, а не с
        # абсолютными процентами.
        "spread_atr": (spread / atr) if atr else None,
        "edge": edge_ratio(atr, spread, rates),
    }


def score(row: dict) -> Decimal:
    """Рейтинг: частые выносы, которые возвращаются, на дешёвой паре.

    Частота и возврат перемножаются, а не складываются: пара с сотней
    выносов, из которых не вернулся ни один, бесполезна ровно так же,
    как пара с единственным вернувшимся. Спред в долях ATR делит
    результат — он вычитается из каждой сделки.
    """
    squeezes = row["squeezes"] or Decimal(0)
    rebound = row["rebound"] or Decimal(0)
    edge = row["edge"] or Decimal(0)

    # Запас над издержками входит в рейтинг, а не только в отсечку:
    # между двумя парами с одинаковой частотой и возвратом лучше та, где
    # от каждой сделки остаётся больше.
    return squeezes * rebound * min(edge, Decimal(10)) / 100


def passes(row: dict, min_trades: int, max_dry: Decimal, min_events: int,
           min_edge: Decimal = DEFAULT_MIN_EDGE) -> bool:
    """Жёсткие отсечки: то, что делает пару непригодной, а не худшей."""
    if row["events"] < min_events:
        return False

    if row["edge"] is None or row["edge"] < min_edge:
        return False

    if row["trades"] is None or row["trades"] < min_trades:
        return False

    if row["dry"] is None or row["dry"] > max_dry:
        return False

    return bool(row["rebound"])


async def gather_history(client, symbols, hours, semaphore) -> dict:
    async def one(symbol):
        async with semaphore:
            try:
                return symbol, await fetch_history(client, symbol, hours)
            except Exception:
                return symbol, []

    return dict(await asyncio.gather(*[one(symbol) for symbol in symbols]))


def render(rows: list[dict], title: str) -> str:
    header = (f"{'пара':<16}{'ATR%':>7}{'выносов/сут':>13}{'возврат%':>10}"
              f"{'цель/издержки':>15}{'сделок/мин':>12}{'пустых%':>9}"
              f"{'спред%':>8}{'оборот/сут':>14}{'рейтинг':>9}")
    lines = [title, header, "-" * len(header)]

    for row in rows:
        def number(value, digits=2):
            return "—" if value is None else f"{value:.{digits}f}"

        turnover = row["turnover"] / 1_000_000
        lines.append(
            f"{row['symbol']:<16}"
            f"{number(row['atr'], 3):>7}"
            f"{number(row['squeezes'], 1):>13}"
            f"{number(row['rebound'], 1):>10}"
            f"{number(row['edge'], 1):>15}"
            f"{number(row['trades'], 0):>12}"
            f"{number(row['dry'], 1):>9}"
            f"{number(row['spread'], 4):>8}"
            f"{f'{turnover:,.0f}M'.replace(',', ' '):>14}"
            f"{number(score(row), 1):>9}"
        )

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Скрининг фьючерсных пар под стратегию 1")
    parser.add_argument("--min-turnover", type=Decimal, default=DEFAULT_MIN_TURNOVER)
    parser.add_argument("--max-spread", type=Decimal, default=DEFAULT_MAX_SPREAD_PERCENT)
    parser.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES)
    parser.add_argument("--max-dry", type=Decimal, default=DEFAULT_MAX_DRY_PERCENT)
    parser.add_argument("--min-events", type=int, default=20,
                        help="минимум выносов за окно, иначе доля возврата случайна")
    parser.add_argument("--min-edge", type=Decimal, default=DEFAULT_MIN_EDGE,
                        help="во сколько раз цель сделки должна покрывать издержки")
    parser.add_argument("--exclude", default="",
                        help="пары через запятую, которые не рассматривать")
    parser.add_argument("--min-history", type=Decimal, default=Decimal("0.9"),
                        help=(
                            "какую долю запрошенного окна пара обязана "
                            "покрывать: свежий листинг — это вкат, а не рынок"
                        ))
    parser.add_argument("--fast-hours", type=float, default=24)
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--shortlist", type=int, default=40)
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    semaphore = asyncio.Semaphore(CONCURRENCY)
    excluded = {item.strip().upper() for item in args.exclude.split(",") if item.strip()}
    rates = await commission_rates()

    async with httpx.AsyncClient(timeout=30) as client:
        candidates = await universe(client, args.min_turnover, args.max_spread)
        candidates = [row for row in candidates if row["symbol"] not in excluded]
        print(f"Шаг 1. Пар с оборотом ≥ {args.min_turnover / 1_000_000:.0f}M и "
              f"спредом ≤ {args.max_spread}%: {len(candidates)}")

        spreads = {row["symbol"]: row["spread"] for row in candidates}
        turnovers = {row["symbol"]: row["turnover"] for row in candidates}
        symbols = [row["symbol"] for row in candidates]

        print(f"Шаг 2. Короткая история {args.fast_hours:.0f} ч по {len(symbols)} парам…")
        history = await gather_history(client, symbols, args.fast_hours, semaphore)

        fast = [
            measure(symbol, candles, spreads[symbol], turnovers[symbol],
                    rates.get(symbol, (FALLBACK_MAKER, FALLBACK_TAKER)))
            for symbol, candles in history.items() if candles
        ]
        fast = [row for row in fast
                if passes(row, args.min_trades, args.max_dry, args.min_events,
                          args.min_edge)]
        fast.sort(key=score, reverse=True)
        shortlist = [row["symbol"] for row in fast[:args.shortlist]]

        print(f"        прошли отсечки: {len(fast)}, в шортлист: {len(shortlist)}")

        print(f"Шаг 3. Глубокая история {args.hours:.0f} ч по шортлисту…")
        deep_history = await gather_history(client, shortlist, args.hours, semaphore)

        wanted_candles = args.hours * 60 * float(args.min_history)
        deep = [
            measure(symbol, candles, spreads[symbol], turnovers[symbol],
                    rates.get(symbol, (FALLBACK_MAKER, FALLBACK_TAKER)))
            for symbol, candles in deep_history.items()
            if len(candles) >= wanted_candles
        ]
        short_history = [
            symbol for symbol, candles in deep_history.items()
            if candles and len(candles) < wanted_candles
        ]
        deep = [row for row in deep
                if passes(row, args.min_trades, args.max_dry,
                          args.min_events * int(args.hours / args.fast_hours),
                          args.min_edge)]
        deep.sort(key=score, reverse=True)

    print()
    print(render(deep[:args.shortlist], f"Рейтинг по {args.hours:.0f} ч:"))

    if short_history:
        print()
        print("Отсеяны как свежий листинг (истории меньше окна): "
              + ", ".join(sorted(short_history)))

    if excluded:
        print("Исключены по требованию: " + ", ".join(sorted(excluded)))

    print()
    print("Ростер:", ",".join(row["symbol"] for row in deep[:args.top]))


if __name__ == "__main__":
    asyncio.run(main())
