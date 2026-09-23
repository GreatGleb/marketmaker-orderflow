"""Края спреда из потока котировок: разбор и однородность пачки.

Заглушки, база и сеть не нужны.

Главное здесь — что набор ключей у записи один и тот же независимо от
источника. `AssetHistoryCrud.bulk_create` собирает пачку одним
многострочным INSERT, и колонки берутся из первой строки, так что
разнородная пачка ломается двумя способами сразу: строка без ключей
первой уносит края спреда у всех остальных молча, а строка без ключей
после строки с ключами роняет вставку целиком. Оба случая проверяются
здесь на настоящем компиляторе SQLAlchemy — база для этого не нужна.
"""
import asyncio

from decimal import Decimal
from unittest.mock import patch

from sqlalchemy import insert
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import CompileError

import app.scripts.watch_ws_and_save as feeder

from app.db.models import AssetHistory
from app.scripts.watch_ws_and_save import (
    BOOK_FIELDS, book_top, merge_book_and_ticker,
)

# Спотовый bookTicker: цены и объёмы приходят строками.
GOOD = {"s": "BMTUSDT", "b": "0.01847", "B": "4120", "a": "0.01849", "A": "3900"}

# Источники без книги: фьючерсный !ticker@arr и REST /fapi/v1/ticker/24hr.
NO_BOOK = {"s": "BMTUSDT", "c": "0.01848", "E": 1758600000000}

BAD_BOOKS = {
    "перевёрнутая книга": {"b": "0.02", "a": "0.01"},
    "ask равен bid": {"b": "0.01848", "a": "0.01848"},
    "нулевой bid": {"b": "0", "a": "0.01849"},
    "отрицательный ask": {"b": "0.01847", "a": "-0.01849"},
    "не число": {"b": "нет", "a": "0.01849"},
    "None вместо цены": {"b": None, "a": "0.01849"},
    "только одна сторона": {"b": "0.01847"},
}


def check_keys_are_always_present():
    for name, item in [("книга есть", GOOD), ("книги нет", NO_BOOK)] + list(BAD_BOOKS.items()):
        keys = set(book_top(item))
        assert keys == set(BOOK_FIELDS), f"{name}: набор ключей {keys}"

    print("  набор ключей одинаков у всех источников")


def check_good_book():
    book = book_top(GOOD)

    assert book["best_bid_price"] == Decimal("0.01847"), book
    assert book["best_ask_price"] == Decimal("0.01849"), book
    assert book["best_bid_qty"] == Decimal("4120"), book
    assert book["best_ask_qty"] == Decimal("3900"), book

    # Ради этого всё и затевалось: спред должен считаться из записи.
    spread = (book["best_ask_price"] - book["best_bid_price"]) / (
        (book["best_ask_price"] + book["best_bid_price"]) / 2
    ) * 100
    assert Decimal("0.10") < spread < Decimal("0.11"), spread

    print("  корректная книга разобрана, спред из неё считается")


def check_missing_and_broken():
    assert all(value is None for value in book_top(NO_BOOK).values())

    for name, fields in BAD_BOOKS.items():
        book = book_top(dict(NO_BOOK, **fields))
        # Отбрасываем обе стороны, а не одну: по половине книги посчитают
        # отрицательный или бесконечный спред и не заметят этого.
        assert all(value is None for value in book.values()), f"{name}: {book}"

    print("  источник без книги и битая книга дают пустые поля целиком")


def check_quantities_are_optional():
    book = book_top({"b": "0.01847", "a": "0.01849"})

    assert book["best_bid_price"] == Decimal("0.01847"), book
    assert book["best_bid_qty"] is None and book["best_ask_qty"] is None, book

    print("  цены сохраняются и без объёмов")


def check_columns_exist():
    columns = set(AssetHistory.__table__.columns.keys())
    missing = set(BOOK_FIELDS) - columns

    assert not missing, f"в asset_history нет колонок: {missing}"

    print("  каждому ключу соответствует колонка asset_history")


def check_batch_is_homogeneous():
    """Ровно та авария, ради которой ключи книги ставятся всегда."""
    row = {"symbol": "BMTUSDT", "source": "BINANCE_SPOT", "last_price": 1}
    with_book = dict(row, best_bid_price=Decimal("1"), best_ask_price=Decimal("2"))

    # Первой идёт строка без книги — вставка проходит, и края спреда у
    # второй строки исчезают без единого слова в логе.
    sql = str(
        insert(AssetHistory).values([row, with_book])
        .compile(dialect=postgresql.dialect())
    )
    assert "best_bid_price" not in sql, (
        "SQLAlchemy перестал молча терять колонки: обоснование BOOK_FIELDS "
        "в watch_ws_and_save.py надо перечитать"
    )

    # Обратный порядок роняет всю пачку, а не одну строку.
    try:
        insert(AssetHistory).values([with_book, row]).compile(
            dialect=postgresql.dialect()
        )
    except CompileError:
        pass
    else:
        raise AssertionError("разнородная пачка скомпилировалась, хотя не должна")

    print("  разнородная пачка ломается — оба способа воспроизведены")


class _FakeSession:
    async def commit(self):
        pass

    async def rollback(self):
        pass


class _FakeCrud:
    """Один класс на все три: симулятору тут нужны только карта пар и вставка."""

    saved = []

    def __init__(self, session):
        pass

    async def get_symbol_to_id_map(self):
        return {"BMTUSDT": 1, "AKEUSDT": 2}

    get_all_symbols_with_id_map = get_symbol_to_id_map

    async def bulk_create(self, records):
        type(self).saved.extend(records)


async def check_record_always_carries_book():
    """Пачка из двух источников сразу: у одного книга есть, у другого нет."""
    mixed = [
        dict(GOOD, c="0.01848", E=1758600000000),
        dict(NO_BOOK, s="AKEUSDT"),
    ]

    _FakeCrud.saved = []

    with patch.object(feeder, "AssetHistoryCrud", _FakeCrud), \
            patch.object(feeder, "WatchedPairCrud", _FakeCrud), \
            patch.object(feeder, "AssetExchangeSpecCrud", _FakeCrud), \
            patch.object(feeder, "parse_snapshot", lambda *a, **k: (1, 1)), \
            patch.object(feeder, "publish_prices", _accept_all):
        await feeder.save_filtered_assets(
            _FakeSession(), _FakeRedis(), mixed, True, source="BINANCE_SPOT",
        )

    assert len(_FakeCrud.saved) == 2, _FakeCrud.saved

    for record in _FakeCrud.saved:
        missing = set(BOOK_FIELDS) - set(record)
        assert not missing, f"в записи нет ключей книги: {missing}"

    with_book, without = _FakeCrud.saved
    assert with_book["best_bid_price"] == Decimal("0.01847"), with_book
    assert without["best_bid_price"] is None, without

    print("  запись в историю несёт ключи книги даже без книги")


async def _accept_all(redis, snapshots):
    return [True] * len(snapshots)


class _FakeRedis:
    async def get(self, key):
        return None


# Фьючерсный @bookTicker: края книги, время события, объёмы уровней.
FUTURES_BOOK = {
    "e": "bookTicker", "s": "BMTUSDT", "E": 1758600000000, "T": 1758600000000,
    "b": "0.01847", "B": "4120", "a": "0.01849", "A": "3900", "u": 77,
}
# Фьючерсный @ticker с того же символа: 24-часовая статистика, без bid/ask.
FUTURES_TICKER = {
    "e": "24hrTicker", "s": "BMTUSDT", "E": 1758599999000, "c": "0.01850",
    "q": "1234567", "v": "42", "p": "0.0001", "P": "0.5",
    "h": "0.02", "l": "0.017", "w": "0.0185", "O": 1, "C": 2,
}


def check_futures_merge():
    """Две подписки в одну строку: цена из книги, оборот из тикера."""
    item = merge_book_and_ticker("BMTUSDT", FUTURES_BOOK, FUTURES_TICKER)

    # Цена — середина спреда, а не последняя сделка из @ticker: по этой
    # величине посчитаны все накопленные сделки, менять её нельзя.
    assert item["c"] == "0.01848", item["c"]
    # Время берётся у книги, а не у тикера: он отстаёт на секунду, и по
    # его метке монотонность в Redis отбросила бы свежую котировку.
    assert item["E"] == FUTURES_BOOK["E"], item["E"]
    assert item["q"] == "1234567", item
    assert book_top(item)["best_bid_qty"] == Decimal("4120"), item

    print("  фьючерсные потоки сливаются в одну котировку")


def check_merge_without_ticker():
    """Статистика приезжает отдельным соединением и в первые мгновения
    отстаёт. Котировку это задержать не должно."""
    item = merge_book_and_ticker("BMTUSDT", FUTURES_BOOK, None)

    assert item["c"] == "0.01848", item
    assert item.get("q") is None, item
    assert book_top(item)["best_ask_price"] == Decimal("0.01849"), item

    print("  котировка пишется и до прихода 24-часовой статистики")


def check_merge_without_book():
    """Без книги строки нет вовсе: цену брать неоткуда."""
    assert merge_book_and_ticker("BMTUSDT", None, FUTURES_TICKER) is None
    assert merge_book_and_ticker("BMTUSDT", {"s": "BMTUSDT"}, FUTURES_TICKER) is None
    assert merge_book_and_ticker("BMTUSDT", {"b": "1"}, None) is None

    print("  без книги котировка не собирается")


def main():
    print("Края спреда в потоке котировок:")
    check_keys_are_always_present()
    check_good_book()
    check_missing_and_broken()
    check_quantities_are_optional()
    check_columns_exist()
    check_batch_is_homogeneous()
    check_futures_merge()
    check_merge_without_ticker()
    check_merge_without_book()
    asyncio.run(check_record_always_carries_book())
    print("Готово.")


if __name__ == "__main__":
    main()
