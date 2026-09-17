"""Общий фильтр номинала MARKET-покупки USDⓈ-M, без маржи и плеча."""
import asyncio
import logging
import time
from dataclasses import dataclass
from decimal import Decimal, DecimalException, ROUND_CEILING, localcontext
from math import gcd, lcm

import httpx

from app.config import settings
from app.constants.markets import TRADABLE_CONTRACT_TYPES, TRADABLE_QUOTE_ASSETS
from app.scripts.seed_binance_data import fetch_binance_data, seed_binance_data

# exchangeInfo не содержит достоверного времени обновления каждого фильтра:
# используем только заново полученный снимок, а не старую строку из БД.
MAX_AGE_SECONDS = 60
FAPI = "https://fapi.binance.com"


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("значение должно быть конечным и положительным")
    return number


def fresh(timestamp, now):
    age = Decimal(str(now)) * 1000 - positive(timestamp)
    if not -5000 <= age <= MAX_AGE_SECONDS * 1000:
        raise ValueError("цена устарела или время находится в будущем")


def minimum_quantity(lots, mark, notional):
    """Пересечение двух сеток (q-minQty) % stepSize == 0 без перебора."""
    bounds = [(positive(f['minQty']), positive(f['maxQty']),
               positive(f['stepSize'])) for f in lots]
    if any(lo > hi for lo, hi, step in bounds):
        raise ValueError("minQty больше maxQty")
    scale = Decimal(10) ** max(-v.as_tuple().exponent for b in bounds for v in b)
    (a, _, m), (b, _, n) = [tuple(int(v * scale) for v in row) for row in bounds]
    common = gcd(m, n)
    if (b - a) % common:
        raise ValueError("сетки LOT_SIZE и MARKET_LOT_SIZE несовместимы")
    modulus = n // common
    k = ((b - a) // common * pow(m // common, -1, modulus)) % modulus if modulus > 1 else 0
    step = Decimal(lcm(m, n)) / scale
    origin = Decimal((a + m * k) % lcm(m, n)) / scale
    lower = max(bounds[0][0], bounds[1][0], notional / mark)
    qty = origin + ((lower - origin) / step).to_integral_value(rounding=ROUND_CEILING) * step
    # Повторная проверка защищает границу после деления Decimal.
    if qty * mark < notional:
        qty += step
    if qty > min(b[1] for b in bounds):
        raise ValueError("минимальная покупка превышает maxQty")
    return qty


def check_purchase(spec, book, mark, fx, now, budget):
    """Возвращает (минимальное количество, причина отказа)."""
    try:
        with localcontext() as ctx:
            ctx.prec = 60
            budget = positive(budget)
            if spec['contractType'] not in TRADABLE_CONTRACT_TYPES or spec['quoteAsset'] not in TRADABLE_QUOTE_ASSETS:
                raise ValueError("неторгуемый тип контракта или котировка")
            if spec['status'] != 'TRADING' or 'MARKET' not in spec['orderTypes']:
                raise ValueError("рынок не TRADING или MARKET недоступен")
            fresh(book['time'], now)
            fresh(mark['time'], now)
            price, mark_price = positive(book['askPrice']), positive(mark['markPrice'])
            rate = Decimal(1)
            if spec['quoteAsset'] == 'USDC':
                fresh(fx['closeTime'], now)
                rate = positive(fx['askPrice'])
            filters = {f['filterType']: f for f in spec['filters']}
            notional = positive(filters['MIN_NOTIONAL']['notional'])
            qty = minimum_quantity([filters['LOT_SIZE'], filters['MARKET_LOT_SIZE']], mark_price, notional)
            cost = qty * max(price, mark_price) * rate
            if cost > budget:
                return None, f"минимальный номинал {cost} USDT > {budget} USDT"
            return qty, None
    except (KeyError, TypeError, ValueError, DecimalException) as error:
        return None, f"нет или некорректны цена/ограничения: {error}"


@dataclass
class Snapshot:
    specs: dict
    books: dict
    marks: dict
    fx: dict
    received_at: float

    def keep(self, symbols):
        now = time.time()
        kept = []
        for symbol in symbols:
            if not 0 <= now - self.received_at <= MAX_AGE_SECONDS:
                reason = "снимок ограничений устарел"
            else:
                _, reason = check_purchase(
                    self.specs.get(symbol), self.books.get(symbol),
                    self.marks.get(symbol), self.fx, now,
                    settings.WATCHED_PAIR_MAX_BUY_NOTIONAL_USDT,
                )
            if reason:
                logging.info("Отбор по бюджету: %s — %s", symbol, reason)
            else:
                kept.append(symbol)
        return kept


def index_rows(rows, source):
    """Неполный или неоднозначный общий ответ нельзя считать свежим снимком."""
    if not isinstance(rows, list):
        raise ValueError(f"{source}: ожидался список")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('symbol'), str) or not row['symbol']:
            raise ValueError(f"{source}: отсутствует корректный symbol")
        if row['symbol'] in indexed:
            raise ValueError(f"{source}: повторный symbol {row['symbol']}")
        indexed[row['symbol']] = row
    return indexed


async def load_snapshot():
    """Фиксированное число публичных запросов на весь рынок, без API-ключей."""
    started = time.time()
    async with httpx.AsyncClient(timeout=30) as client:
        async def get(url):
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

        results = await asyncio.gather(
            fetch_binance_data(),
            get(FAPI + '/fapi/v1/ticker/bookTicker'),
            get(FAPI + '/fapi/v1/premiumIndex'),
            get('https://api.binance.com/api/v3/ticker/24hr?symbol=USDCUSDT'),
            return_exceptions=True,
        )
    data, books, marks, fx = results
    for payload in (data, books, marks):
        if isinstance(payload, Exception):
            logging.error("Нет свежего снимка Binance: %s", payload)
            return Snapshot({}, {}, {}, {}, started)
    if isinstance(fx, Exception):
        logging.warning("Нет курса USDC/USDT: %s; USDC-пары будут исключены", fx)
        fx = {}
    # Ошибочный JSON не должен оборвать удаление недоступных старых пар.
    try:
        specs_by_symbol = index_rows(data['symbols'], 'exchangeInfo')
        books_by_symbol = index_rows(books, 'bookTicker')
        marks_by_symbol = index_rows(marks, 'premiumIndex')
    except (KeyError, TypeError, ValueError) as error:
        logging.error("Некорректный снимок Binance: %s", error)
        return Snapshot({}, {}, {}, {}, started)
    # Тот же seed обновляет справочник, а фильтр читает исходные Decimal-строки.
    await seed_binance_data(data=data)
    return Snapshot(
        specs_by_symbol, books_by_symbol, marks_by_symbol, fx, started,
    )


async def affordable_symbols(session, symbols):
    snapshot = session.info.get('watched_affordability')
    if snapshot is None or not 0 <= time.time() - snapshot.received_at <= MAX_AGE_SECONDS:
        snapshot = await load_snapshot()
        session.info['watched_affordability'] = snapshot
    return snapshot.keep(symbols)
