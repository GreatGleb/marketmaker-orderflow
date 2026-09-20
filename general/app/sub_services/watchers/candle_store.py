"""Минутные свечи в Redis: единый формат записи и чтения.

Раньше здесь лежал просто список цен закрытия — всё, что нужно MA-ботам.
Стратегиям, считающим волатильность (ATR, отношение тени к телу), нужны
все четыре цены, поэтому формат расширен до OHLC.

Старый формат (список скаляров) читается по-прежнему: в Redis могли
остаться ключи с прошлого запуска, а MA-боты не должны от этого падать.
Свеча из такого списка отдаётся с `o = h = l = c` и без времени — по ней
считается MA, но не размах.
"""

import json
import time
from decimal import Decimal, InvalidOperation

# Сколько свечей держать на пару, если MA-ботам нужно меньше. ATR(14) и
# процентиль по нему считаются на этом же ключе, отсюда запас.
MIN_HISTORY = 60

# Свечи старше — не данные, а память о питателе, который встал. Десять
# минут: при минутной свече это уже явный простой, а не задержка.
MAX_AGE_SECONDS = 600


def is_stale(candles, max_age_seconds: float = MAX_AGE_SECONDS, now_ms: int | None = None) -> bool:
    """Свечи устарели или их нет.

    Время последней свечи неизвестно (старый формат) — считаем
    устаревшими: утверждать свежесть не на чем, а ATR по мёртвым данным
    хуже, чем его отсутствие.
    """
    if not candles:
        return True

    last_time = candles[-1].get("t")

    if last_time is None:
        return True

    now_ms = int(time.time() * 1000) if now_ms is None else now_ms

    return now_ms - int(last_time) > max_age_seconds * 1000


def candles_key(symbol: str) -> str:
    return f"candles:{symbol}"


def make_candle(open_time_ms, open_, high, low, close, volume=None, trades=None) -> dict:
    """Свеча как её кладут в Redis: цены строками, время — мс открытия.

    Строки, а не float: цена приходит от биржи строкой, и промежуточный
    float терял бы точность у монет с восемью знаками.

    `trades` — число сделок в минуте. Прямая мера тиковой плотности: на
    паре, где минута проходит без единой сделки, лимитка не исполнится,
    сколько бы ни казался привлекательным её ATR.
    """
    candle = {
        "t": int(open_time_ms),
        "o": str(open_),
        "h": str(high),
        "l": str(low),
        "c": str(close),
    }

    if volume is not None:
        candle["v"] = str(volume)

    if trades is not None:
        candle["n"] = int(trades)

    return candle


def candle_from_kline(kline) -> dict:
    """Свеча из ответа REST `/klines`.

    Формат: [openTime, o, h, l, c, v, closeTime, quoteVolume, trades, ...].
    """
    trades = kline[8] if len(kline) > 8 else None

    return make_candle(kline[0], kline[1], kline[2], kline[3], kline[4],
                       kline[5], trades)


def candle_from_ws_kline(data: dict) -> dict:
    """Свеча из поля `k` потока `@kline_1m`."""
    return make_candle(data["t"], data["o"], data["h"], data["l"], data["c"],
                       data.get("v"), data.get("n"))


def load_candles(raw) -> list[dict]:
    """Разбирает содержимое ключа. Непонятное значение — пустая история.

    Мусор в ключе не повод ронять бота: свечи наберутся заново за минуту,
    а исключение в горячем цикле стоит дороже.
    """
    if not raw:
        return []

    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return []

    if not isinstance(items, list):
        return []

    candles = []

    for item in items:
        if isinstance(item, dict):
            if {"o", "h", "l", "c"} <= item.keys():
                candles.append(item)
        elif isinstance(item, (str, int, float)):
            # Старый формат: известна только цена закрытия.
            candles.append({"t": None, "o": str(item), "h": str(item),
                            "l": str(item), "c": str(item)})

    return candles


def dump_candles(candles: list[dict]) -> str:
    return json.dumps(candles)


def merge_candle(candles: list[dict], candle: dict, limit: int) -> list[dict]:
    """Добавляет свечу, храня историю упорядоченной и без дублей.

    Повтор той же минуты заменяет запись, а не удлиняет историю: REST
    отдаёт перекрывающиеся окна, и без этого ключ распух бы копиями.
    """
    result = [item for item in candles if item.get("t") != candle["t"]]
    result.append(candle)
    result.sort(key=lambda item: (item.get("t") is None, item.get("t") or 0))

    if limit and len(result) > limit:
        result = result[-limit:]

    return result


def _decimal(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None

    return number if number.is_finite() else None


def closes(candles: list[dict]) -> list[Decimal]:
    """Цены закрытия — то, на чём считают свои средние MA-боты."""
    values = [_decimal(candle.get("c")) for candle in candles]

    return [value for value in values if value is not None]


def closes_from_raw(raw) -> list[Decimal]:
    return closes(load_candles(raw))


def true_range(candle: dict, previous_close: Decimal | None) -> Decimal | None:
    """Истинный диапазон свечи: размах с учётом гэпа от прошлого закрытия."""
    high = _decimal(candle.get("h"))
    low = _decimal(candle.get("l"))

    if high is None or low is None:
        return None

    if previous_close is None:
        return high - low

    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def atr(candles: list[dict], period: int) -> Decimal | None:
    """ATR по Уайлдеру. None — истории меньше, чем нужно для периода.

    Сглаживание именно уайлдеровское, а не простое среднее: так ATR
    считают везде, и подобранный на нём множитель отступа не придётся
    пересчитывать при сверке с любым сторонним графиком.

    У свечей старого формата (одни закрытия) размах равен нулю, и ATR
    выходит заниженным — он там считается по гэпам между закрытиями.
    Это цена обратной совместимости ключа, а не ошибка расчёта.
    """
    ranges = true_ranges(candles)

    if ranges is None or period < 1 or len(ranges) < period:
        return None

    result = sum(ranges[:period], Decimal(0)) / Decimal(period)

    for value in ranges[period:]:
        result = (result * (period - 1) + value) / Decimal(period)

    return result


def true_ranges(candles: list[dict]) -> list[Decimal] | None:
    """Истинные диапазоны всей истории. None — в ряду битая свеча.

    Первую свечу пропускаем: её диапазон посчитан без предыдущего
    закрытия, то есть по другому правилу, чем остальные.

    Битая свеча в середине обнуляет расчёт целиком: склеивать разорванный
    ряд — значит считать ATR по выдуманным данным.
    """
    ranges = []
    previous_close = None

    for candle in candles:
        value = true_range(candle, previous_close)
        close = _decimal(candle.get("c"))

        if value is None or close is None:
            return None

        if previous_close is not None:
            ranges.append(value)

        previous_close = close

    return ranges


def atr_percent(candles: list[dict], period: int) -> Decimal | None:
    """ATR в процентах от последнего закрытия.

    Стратегии считают отступы в процентах: одна и та же величина в
    абсолюте на паре за 80 000 и на паре за 0.019 означает разное.
    """
    value = atr(candles, period)

    if value is None:
        return None

    last_close = _decimal(candles[-1].get("c"))

    if not last_close:
        return None

    return value / last_close * Decimal(100)


def atr_series(candles: list[dict], period: int) -> list[Decimal]:
    """ATR на каждом шаге истории — распределение, а не одно значение.

    Нужен процентилю: «высокий ATR» имеет смысл только относительно того,
    каким этот ATR у пары бывает обычно.
    """
    ranges = true_ranges(candles)

    if ranges is None or period < 1 or len(ranges) < period:
        return []

    value = sum(ranges[:period], Decimal(0)) / Decimal(period)
    series = [value]

    for item in ranges[period:]:
        value = (value * (period - 1) + item) / Decimal(period)
        series.append(value)

    return series


def percentile_of(series: list[Decimal], value: Decimal) -> Decimal | None:
    """Место значения в собственном ряду, в процентах.

    Считается доля значений, не превышающих данное. Именно «не
    превышающих», а не «меньших»: текущий ATR сам входит в ряд, и при
    строгом сравнении даже исторический максимум давал бы не 100, а
    чуть меньше — тем меньше, чем короче история.
    """
    if not series or value is None:
        return None

    below = sum(1 for item in series if item <= value)

    return Decimal(below) / Decimal(len(series)) * Decimal(100)


def shadow_to_body(candle: dict) -> Decimal | None:
    """Отношение теней к телу свечи.

    Чем длиннее тени при плотном теле, тем лучше пара отрабатывает
    прострелы: цену выносит и возвращает внутри одной минуты.

    У свечи с нулевым телом (открытие равно закрытию) отношение было бы
    бесконечным, поэтому телом считается хотя бы один шаг цены — здесь
    это размах, делённый на сто, то есть потолок отношения равен ста.
    """
    open_ = _decimal(candle.get("o"))
    high = _decimal(candle.get("h"))
    low = _decimal(candle.get("l"))
    close = _decimal(candle.get("c"))

    if None in (open_, high, low, close):
        return None

    span = high - low

    if span <= 0:
        return None

    body = abs(close - open_)
    shadows = span - body
    floor = span / Decimal(100)

    return shadows / max(body, floor)


def spike_count(candles: list[dict], move_percent: Decimal) -> int:
    """Сколько свечей с прострелом: тень длиннее порога, % от цены.

    Считается именно тень, а не размах свечи: прострел — это вынос,
    который откупили внутри той же минуты. Свеча, целиком ушедшая вниз,
    такой же длины — это тренд, и стратегии он не нужен.
    """
    count = 0

    for candle in candles:
        high = _decimal(candle.get("h"))
        low = _decimal(candle.get("l"))
        open_ = _decimal(candle.get("o"))
        close = _decimal(candle.get("c"))

        if None in (high, low, open_, close) or not close:
            continue

        upper = high - max(open_, close)
        lower = min(open_, close) - low
        longest = max(upper, lower) / close * Decimal(100)

        if longest >= move_percent:
            count += 1

    return count


def shadow_share(candles: list[dict], ratio: Decimal = Decimal(1)) -> Decimal | None:
    """Доля свечей, где тени длиннее тела в `ratio` раз, в процентах.

    Сводить `shadow_to_body` средним или медианой бессмысленно:
    распределение с длинным хвостом — у свечи с почти нулевым телом
    отношение улетает в сотни, а медиана на трендовой паре садится в
    ноль. Доля отвечает на вопрос прямо: в скольких минутах из ста цену
    выносило и возвращало обратно.
    """
    values = [value for value in (shadow_to_body(candle) for candle in candles)
              if value is not None]

    if not values:
        return None

    return Decimal(sum(1 for value in values if value >= ratio)) / Decimal(len(values)) * Decimal(100)


def squeeze_events(candles: list[dict], period: int,
                   atr_multiple: Decimal = Decimal(2)) -> list[dict]:
    """Выносы: уход цены от прошлого закрытия дальше `atr_multiple` × ATR.

    Событие определяется через расстояние от предыдущего закрытия до
    экстремума минуты, а не через длину тени. Это не придирка к
    определению, а единственный способ увидеть «падающий нож»: тень
    отсчитывается от тела свечи и потому сама по себе означает возврат —
    по ней вынос без возврата неотличим от обычной трендовой свечи.

    Так же стоит и заявка стратегии: на отступе в ATR от текущей цены, а
    не от тела будущей свечи. Событие здесь — это в точности «цена
    дошла до нашего уровня».

    ATR скользящий: порог должен означать одно и то же в тихие часы и в
    разгон, иначе все события соберутся в одном куске суток.
    """
    series = atr_series(candles, period)

    if not series:
        return []

    events = []

    for offset, atr_value in enumerate(series):
        # Первый элемент ряда соответствует свече с индексом `period`.
        index = period + offset
        candle = candles[index]

        high = _decimal(candle.get("h"))
        low = _decimal(candle.get("l"))
        close = _decimal(candle.get("c"))
        previous = _decimal(candles[index - 1].get("c"))

        if None in (high, low, close, previous) or atr_value <= 0:
            continue

        threshold = atr_value * atr_multiple
        down = previous - low
        up = high - previous

        if down >= threshold and down >= up:
            events.append({"index": index, "side": "down", "length": down,
                           "extreme": low, "close": close, "from": previous})
        elif up >= threshold:
            events.append({"index": index, "side": "up", "length": up,
                           "extreme": high, "close": close, "from": previous})

    return events


def rebound_share(candles: list[dict], period: int,
                  atr_multiple: Decimal = Decimal(2),
                  fraction: Decimal = Decimal("0.3"),
                  horizon: int = 2) -> tuple[int, Decimal | None]:
    """Сколько выносов вернулось и какая это доля.

    Возврат считается от экстремума обратно к цене, от которой цену
    вынесло: нужно пройти `fraction` этого пути — внутри той же минуты
    (по её закрытию) либо за ближайшие `horizon` свечей. Это ровно то,
    на чём зарабатывает стратегия, и то, чего не бывает у «падающего
    ножа», где вынос переходит в тренд.

    Возвращает число событий и долю вернувшихся в процентах. Доля без
    числа событий обманчива: три выноса из трёх — это 100% и ничего.
    """
    events = squeeze_events(candles, period, atr_multiple)

    if not events:
        return 0, None

    recovered = 0

    for event in events:
        target = (
            event["extreme"] + event["length"] * fraction
            if event["side"] == "down"
            else event["extreme"] - event["length"] * fraction
        )

        # Свеча выноса закрылась уже за целью — возврат случился внутри
        # той же минуты. Для стратегии это лучший исход: позиция живёт
        # секунды, и ждать следующей свечи ей незачем.
        if event["side"] == "down" and event["close"] >= target:
            recovered += 1
            continue

        if event["side"] == "up" and event["close"] <= target:
            recovered += 1
            continue

        for candle in candles[event["index"] + 1:event["index"] + 1 + horizon]:
            high = _decimal(candle.get("h"))
            low = _decimal(candle.get("l"))

            if high is None or low is None:
                continue

            if event["side"] == "down" and high >= target:
                recovered += 1
                break

            if event["side"] == "up" and low <= target:
                recovered += 1
                break

    return len(events), Decimal(recovered) / Decimal(len(events)) * Decimal(100)


def trade_density(candles: list[dict]) -> tuple[Decimal | None, Decimal | None]:
    """Медиана сделок в минуту и доля минут без единой сделки.

    Пара, где каждая пятая минута пустая, не годится: лимитка простоит
    там весь тайм-стоп, а рыночный выход исполнится по любой цене.
    """
    counts = [candle.get("n") for candle in candles]
    counts = [int(value) for value in counts if value is not None]

    if not counts:
        return None, None

    counts.sort()
    middle = counts[len(counts) // 2]
    dry = sum(1 for value in counts if value == 0)

    return Decimal(middle), Decimal(dry) / Decimal(len(counts)) * Decimal(100)
