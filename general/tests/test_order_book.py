"""Слушатель стакана: разбор снимка, OBI, суточная нарезка архива.

Голые assert, без pytest. Сеть, база и Redis не нужны — проверяется то,
что считается из готового фрейма.
"""
import gzip
import json
import tempfile
from decimal import Decimal
from pathlib import Path

from app.scripts.order_book_listener import (
    DailyArchive,
    archive_path,
    order_book_imbalance,
    snapshot_from_payload,
    split_streams,
    utc_day,
)


def frame(bids, asks, symbol="ALGOUSDT", event_ms=1790199020720):
    return {
        "e": "depthUpdate", "E": event_ms, "T": event_ms - 40, "s": symbol,
        "b": bids, "a": asks,
    }


def test_snapshot_parsing():
    data = frame([["0.1045", "100"], ["0.1044", "200"]],
                 [["0.1046", "300"], ["0.1047", "400"]])
    snapshot = snapshot_from_payload(data, 20)

    assert snapshot["symbol"] == "ALGOUSDT"
    assert snapshot["event_time_ms"] == 1790199020720
    assert snapshot["transaction_time_ms"] == 1790199020680
    assert snapshot["bids"] == [["0.1045", "100"], ["0.1044", "200"]]
    assert snapshot["asks"] == [["0.1046", "300"], ["0.1047", "400"]]

    # Уровни режутся до запрошенных: ORDER_BOOK_LEVELS меньше двадцати
    # ставят ради места на диске, и обрезать должно именно здесь.
    short = snapshot_from_payload(data, 1)
    assert short["bids"] == [["0.1045", "100"]]
    assert short["asks"] == [["0.1046", "300"]]


def test_snapshot_rejects_one_sided():
    """Односторонняя книга — не снимок.

    Такое приходит первым фреймом на только что листнутой паре. Писать
    его незачем: ни OBI, ни спреда по нему не посчитать, а в архиве он
    выглядит как полноценная строка.
    """
    assert snapshot_from_payload(frame([], [["1", "1"]]), 20) is None
    assert snapshot_from_payload(frame([["1", "1"]], []), 20) is None
    assert snapshot_from_payload(frame([["1", "1"]], [["2", "2"]], symbol=None), 20) is None
    assert snapshot_from_payload({}, 20) is None


def test_snapshot_skips_broken_levels():
    data = frame([["0.1", "10"], ["0.09"], "мусор"], [["0.11", "5"]])
    snapshot = snapshot_from_payload(data, 20)

    assert snapshot["bids"] == [["0.1", "10"]]


def test_imbalance_sides():
    bids = [["1.0", "300"]]
    asks = [["1.1", "100"]]

    assert order_book_imbalance(bids, asks, 1) == Decimal("0.5")
    assert order_book_imbalance(asks, bids, 1) == Decimal("-0.5")
    assert order_book_imbalance([["1", "50"]], [["2", "50"]], 1) == 0


def test_imbalance_counts_only_requested_levels():
    """Глубина — это параметр, а не то, что пришло в фрейме.

    Порог по OBI на первом уровне и на двадцати — разные признаки, и
    считаться они должны по одному и тому же числу уровней, иначе
    сравнивать нечего.
    """
    bids = [["1.0", "10"], ["0.9", "990"]]
    asks = [["1.1", "10"], ["1.2", "10"]]

    assert order_book_imbalance(bids, asks, 1) == 0
    assert order_book_imbalance(bids, asks, 2) == Decimal("980") / Decimal("1020")


def test_imbalance_none_when_nothing_to_count():
    """Ноль означал бы равновесие — это другое утверждение.

    Пустая книга и книга с нулевым объёмом — отсутствие данных. Вернуть
    ноль значит сказать «плотность одинаковая», и гейт по такому
    признаку сработает там, где признака нет вовсе.
    """
    assert order_book_imbalance([], [], 5) is None
    assert order_book_imbalance([["1", "0"]], [["2", "0"]], 5) is None
    assert order_book_imbalance([["1", "нечисло"]], [["2", "1"]], 5) is None


def test_split_streams():
    """Стримы раскладываются по сокетам без потерь и без пустых групп.

    Одно соединение на все пары упирается в потолок около двадцати
    сообщений в секунду независимо от числа стримов (замер 2026-09-24),
    поэтому раскладка обязана сохранять **все** стримы: потерянная
    группа — это молча не собираемые пары.
    """
    streams = [f"pair{i}@depth20@100ms" for i in range(11)]
    groups = split_streams(streams, 5)

    assert [len(g) for g in groups] == [5, 5, 1]
    assert [s for g in groups for s in g] == streams

    # Ровное деление не даёт пустого хвоста.
    assert [len(g) for g in split_streams(streams[:10], 5)] == [5, 5]
    # Группа больше набора — один сокет, а не десять пустых.
    assert split_streams(streams[:3], 50) == [streams[:3]]
    # Ноль и минус — прежнее поведение, всё одним сокетом.
    assert split_streams(streams, 0) == [streams]
    assert split_streams(streams, -1) == [streams]
    # Пустой набор не порождает сокета, которому не на что подписываться.
    assert split_streams([], 5) == []


def test_utc_day_from_event_time():
    """Сутки берутся из времени биржи, а не из часов машины.

    Иначе на границе суток строки разъезжаются по двум файлам из-за
    задержки сети, и суточный файл перестаёт быть суточным.
    """
    # 2026-08-31 23:59:59.900 UTC
    assert utc_day(1788220799900) == "2026-08-31"
    # 2026-09-01 00:00:00.100 UTC
    assert utc_day(1788220800100) == "2026-09-01"
    assert len(utc_day(None)) == len("2026-09-01")


def test_archive_layout_and_rotation():
    """Раскладка повторяет бакет Binance, сутки режутся по событию."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        assert archive_path(root, "ALGOUSDT", "2026-09-01") == (
            root / "ALGOUSDT" / "ALGOUSDT-depth20-2026-09-01.jsonl.gz"
        )

        archive = DailyArchive(root)
        late = snapshot_from_payload(
            frame([["1", "2"]], [["3", "4"]], event_ms=1788220799900), 20
        )
        early = snapshot_from_payload(
            frame([["5", "6"]], [["7", "8"]], event_ms=1788220800100), 20
        )
        archive.write(late)
        archive.write(early)
        archive.close()

        assert archive.written == 2

        first = archive_path(root, "ALGOUSDT", "2026-08-31")
        second = archive_path(root, "ALGOUSDT", "2026-09-01")
        assert first.exists() and second.exists()

        with gzip.open(first, "rt") as handle:
            lines = [json.loads(line) for line in handle]

        assert len(lines) == 1
        assert lines[0]["b"] == [["1", "2"]]
        assert lines[0]["T"] == 1788220799860
        # Символ лежит в имени файла, в строке его не повторяем.
        assert "symbol" not in lines[0] and "s" not in lines[0]


def test_archive_reopens_after_close():
    """Закрытый файл дописывается, а не затирается.

    Слушателя перезапускают руками и по supervisorctl; если открывать
    суточный файл на 'w', перезапуск в середине суток сотрёт всё, что
    собрано с утра.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        snapshot = snapshot_from_payload(frame([["1", "2"]], [["3", "4"]]), 20)

        for _ in range(2):
            archive = DailyArchive(root)
            archive.write(snapshot)
            archive.close()

        path = archive_path(root, "ALGOUSDT", utc_day(snapshot["event_time_ms"]))

        with gzip.open(path, "rt") as handle:
            assert len(handle.readlines()) == 2


if __name__ == "__main__":
    test_snapshot_parsing()
    test_snapshot_rejects_one_sided()
    test_snapshot_skips_broken_levels()
    test_imbalance_sides()
    test_imbalance_counts_only_requested_levels()
    test_imbalance_none_when_nothing_to_count()
    test_split_streams()
    test_utc_day_from_event_time()
    test_archive_layout_and_rotation()
    test_archive_reopens_after_close()
    print("Проверки стакана прошли.")
