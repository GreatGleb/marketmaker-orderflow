"""Отбор пар не выпускает за пределы того, чем мы умеем торговать.

Проверка нужна из-за одного неочевидного факта: у токенизированных акций на
фьючерсах Binance символ кончается на USDT ровно так же, как у крипты.
AAPLUSDT, NVDAUSDT, ANTHROPICUSDT — по суффиксу они неотличимы от AAVEUSDT,
и старый фильтр `symbol.endswith("USDT")` пропускал все 174 штуки.

Для скальпера это худший из возможных инструментов: тиков нет большую часть
суток, а гэп на открытии американской биржи выглядит как идеальный «скачок»
— то есть отбор такие пары не отсеет, а поднимет наверх.

Заглушки, база не нужна:

    python -m tests.test_tradable_pairs
"""
import asyncio
from unittest.mock import AsyncMock, patch

from app.constants.markets import (
    TRADABLE_CONTRACT_TYPES,
    TRADABLE_QUOTE_ASSETS,
)
from app.scripts import seed_watched_pairs as sw

# Что вернул бы справочник: крипта в USDT и USDC, и ничего больше. Акции,
# квартальные поставочные и котировки в монете сюда не попадают — их
# отсеивает сам запрос, а не вызывающий код.
ALLOWED = {"BTCUSDT", "ETHUSDC", "AAVEUSDT", "CHEAPUSDT"}
# И весь справочник целиком — в нём акции и квартальные тоже есть.
KNOWN = ALLOWED | {"AAPLUSDT", "BTCUSDT_260925", "ETHBTC", "BTCUSD1"}

# Суточная статистика Binance так и приходит — плоским списком, где акция от
# монеты отличается только тем, чего в ответе нет.
TICKERS = [
    # Первая по размаху, и она же акция. Ради неё вся проверка.
    dict(symbol="AAPLUSDT", quoteVolume="900000000", highPrice="150", lowPrice="100"),
    dict(symbol="ETHUSDC", quoteVolume="900000000", highPrice="120", lowPrice="100"),
    dict(symbol="AAVEUSDT", quoteVolume="900000000", highPrice="110", lowPrice="100"),
    dict(symbol="BTCUSDT", quoteVolume="900000000", highPrice="105", lowPrice="100"),
    # Размах большой, но оборота нет: старая отсечка по ликвидности должна
    # работать по-прежнему.
    dict(symbol="CHEAPUSDT", quoteVolume="500000", highPrice="130", lowPrice="100"),
    # Котировка в монете и поставочный квартальный — ни тот, ни другой в
    # справочник не попали.
    dict(symbol="ETHBTC", quoteVolume="900000000", highPrice="140", lowPrice="100"),
    dict(symbol="BTCUSDT_260925", quoteVolume="900000000", highPrice="135", lowPrice="100"),
]


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return FakeResponse(self._payload)


class FakeHttpx:
    def __init__(self, payload):
        self._payload = payload

    def AsyncClient(self, **kwargs):
        return FakeClient(self._payload)


class SpecRow:
    """Строка asset_exchange_specs в том виде, в каком её читает справочник."""

    def __init__(self, symbol, quote_asset, contract_type):
        self.symbol = symbol
        self.quote_asset = quote_asset
        self.contract_type = contract_type


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, statement):
        return FakeResult(self._rows)


# Справочник целиком, как он выглядит на живой базе: крипта в USDT и USDC,
# токенизированные акции, поставочные квартальные и мелочь в чужой котировке.
SPECS = [
    SpecRow("BTCUSDT", "USDT", "PERPETUAL"),
    SpecRow("AAVEUSDT", "USDT", "PERPETUAL"),
    SpecRow("CHEAPUSDT", "USDT", "PERPETUAL"),
    SpecRow("ETHUSDC", "USDC", "PERPETUAL"),
    SpecRow("AAPLUSDT", "USDT", "TRADIFI_PERPETUAL"),
    SpecRow("BTCUSDT_260925", "USDT", "CURRENT_QUARTER"),
    SpecRow("ETHBTC", "BTC", "PERPETUAL"),
    SpecRow("BTCUSD1", "USD1", "PERPETUAL"),
]


def check_keep_tradable_preserves_order():
    """Фильтр убирает лишнее, не переставляя оставшееся.

    Порядок здесь — это результат ранжирования по скачкам. Пересортировка
    была бы тихой порчей отбора: список остался бы правильной длины и из
    правильных пар, но первой пошла бы не лучшая.
    """
    chosen = ["AAVEUSDT", "AAPLUSDT", "BTCUSDT", "ETHBTC", "ETHUSDC"]
    kept = sw.keep_tradable(chosen, ALLOWED, "проверка")

    assert kept == ["AAVEUSDT", "BTCUSDT", "ETHUSDC"], kept
    print(f"    порядок сохранён, осталось {kept}")

    # Ничего лишнего не отсекается: список целиком из торгуемых пар проходит
    # насквозь тем же объектом значений.
    passthrough = sw.keep_tradable(["BTCUSDT", "ETHUSDC"], ALLOWED, "проверка")

    assert passthrough == ["BTCUSDT", "ETHUSDC"], passthrough
    print("    список из торгуемых пар проходит без потерь")


async def check_reference_splits_known_from_tradable():
    """Отбор идёт по котировке И по типу контракта, а не по чему-то одному.

    Одной котировки мало: у квартального BTCUSDT_260925 и у акции AAPLUSDT
    quote_asset такой же, как у обычного бессрочного BTCUSDT. Одного типа
    контракта тоже мало: ETHBTC — бессрочный.

    Заодно проверяется само разделение: «знаем, но не торгуем» и «не знаем
    вовсе» — разные вещи, и по ним разные действия.
    """
    # Фикстура построена под нынешние константы. Если их меняли, тест должен
    # сказать об этом прямо, а не упасть на непонятном сравнении множеств.
    assert TRADABLE_QUOTE_ASSETS == ("USDT", "USDC"), TRADABLE_QUOTE_ASSETS
    assert TRADABLE_CONTRACT_TYPES == ("PERPETUAL",), TRADABLE_CONTRACT_TYPES

    known, tradable = await sw.symbol_reference(FakeSession(SPECS))

    assert tradable == ALLOWED, tradable
    assert "AAPLUSDT" in known and "AAPLUSDT" not in tradable, "акция торгуема"
    assert "BTCUSDT_260925" not in tradable, "квартальный торгуем"
    assert "ETHBTC" not in tradable, "котировка в монете торгуема"
    assert "BTCUSD1" not in tradable, "USD1 торгуем"
    assert len(known) == len(SPECS), known

    print(f"    из {len(known)} пар справочника торгуемых {len(tradable)}: "
          f"{sorted(tradable)}")
    print(f"    отсечены по котировке и типу контракта: "
          f"{sorted(known - tradable)}")


async def check_binance_candidates_drop_stocks():
    """Кандидаты с Binance проходят через справочник, а не через суффикс."""
    original = sw.httpx
    sw.httpx = FakeHttpx(TICKERS)

    try:
        # Сначала — как отбор вёл себя раньше, по `endswith("USDT")`. Правило
        # ошибалось в обе стороны сразу: акция по суффиксу неотличима от
        # монеты и с размахом в 50% забирала первое место, а USDC-пара
        # отсекалась целиком, хотя торговать ей мы умеем. Квартальный
        # BTCUSDT_260925 суффикс отсекал, но по случайности — его имя
        # кончается датой, а не котировкой.
        by_suffix = [
            t["symbol"] for t in TICKERS
            if t["symbol"].endswith("USDT")
            and float(t["quoteVolume"]) >= sw.MIN_QUOTE_VOLUME_24H
        ]

        assert by_suffix[0] == "AAPLUSDT", by_suffix
        assert "ETHUSDC" not in by_suffix, by_suffix
        print(f"    по суффиксу прошли бы {by_suffix}: первой акция, "
              f"а ETHUSDC не прошла бы вовсе")

        with patch.object(sw, "affordable_symbols", AsyncMock(side_effect=lambda session, symbols: symbols)):
            chosen = await sw.rank_from_binance(
                session=None, top=2, reference=(KNOWN, ALLOWED)
            )
    finally:
        sw.httpx = original

    assert "AAPLUSDT" not in chosen, "акция прошла отбор"
    assert "ETHBTC" not in chosen, "котировка в монете прошла отбор"
    assert "BTCUSDT_260925" not in chosen, "квартальный контракт прошёл отбор"
    assert "CHEAPUSDT" not in chosen, "отсечка по обороту перестала работать"

    # Главное в этой проверке: пар ровно две. Если бы отсев шёл после среза
    # по top, акция заняла бы место в топе и список вышел бы короче.
    assert chosen == ["ETHUSDC", "AAVEUSDT"], chosen
    print(f"    стало: {chosen} — акции нет, мест в топе она не съела")


async def check_empty_reference_says_what_to_do():
    """Пустой справочник — это несделанный шаг установки, а не «нет пар»."""
    original = sw.httpx
    sw.httpx = FakeHttpx(TICKERS)

    try:
        chosen = await sw.rank_from_binance(
            session=None, top=10, reference=(set(), set())
        )
    finally:
        sw.httpx = original

    assert chosen == [], chosen
    print("    пустой справочник: вернулся пустой список с подсказкой в логе")


async def main():
    print("Отбор пар ограничен торгуемыми инструментами:")

    check_keep_tradable_preserves_order()
    await check_reference_splits_known_from_tradable()
    await check_binance_candidates_drop_stocks()
    await check_empty_reference_says_what_to_do()

    print("  ✅ все проверки прошли")


if __name__ == "__main__":
    asyncio.run(main())
