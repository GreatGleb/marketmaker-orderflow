"""Парк «по пробивам»: сетка тиковых ботов.

Сетка восстановлена из сида, действовавшего до e002fe6, и уровни в ней
менять нельзя просто так: по ним накоплена статистика, и другой набор
сделает новый парк несопоставимым со старым.
"""
from collections import Counter

from app.constants.demo_seed import (
    TICK_BOT_SYMBOL,
    TICK_START_VALUES,
    TICK_STOP_LOSS_VALUES,
    TICK_STOP_WIN_VALUES,
    TICK_TRAILING_VALUES,
    TICK_WAIT_SECONDS_VALUES,
    tick_bot_rows,
)

EXPECTED = (len(TICK_START_VALUES) * len(TICK_STOP_LOSS_VALUES)
            * len(TICK_STOP_WIN_VALUES) * len(TICK_TRAILING_VALUES)
            * len(TICK_WAIT_SECONDS_VALUES))


def test_grid_is_full_and_unchanged():
    rows = tick_bot_rows()

    assert len(rows) == EXPECTED == 15120, (
        f"в сетке {len(rows)} комбинаций: статистика накоплена по 15 120, и "
        f"другой набор с ней несопоставим"
    )

    keys = {
        (row["start_updown_ticks"], row["stop_loss_ticks"],
         row["stop_success_ticks"], row["use_trailing_stop"],
         row["time_to_wait_for_entry_price_to_open_order_in_seconds"])
        for row in rows
    }
    assert len(keys) == EXPECTED, "в сетке есть повторы"


def test_levels_are_in_ticks_not_percents():
    """Это и отличает парк от процентного: проценты должны быть пусты."""
    for row in tick_bot_rows():
        assert row.get("start_updown_percents") is None
        assert row.get("stop_loss_percents") is None
        assert row.get("min_timeframe_asset_volatility") is None, (
            "тиковый парк не выбирает пару на ходу: тик у каждой пары свой"
        )


def test_single_pair():
    symbols = {row["symbol"] for row in tick_bot_rows()}

    assert symbols == {TICK_BOT_SYMBOL}
    assert tick_bot_rows("OTHERUSDT")[0]["symbol"] == "OTHERUSDT"


def test_each_level_is_equally_represented():
    """Ни один уровень не должен быть представлен чаще другого."""
    rows = tick_bot_rows()

    for field, values in (
        ("start_updown_ticks", TICK_START_VALUES),
        ("stop_loss_ticks", TICK_STOP_LOSS_VALUES),
        ("stop_success_ticks", TICK_STOP_WIN_VALUES),
    ):
        counts = Counter(row[field] for row in rows)

        assert set(counts) == set(values), f"{field}: набор уровней изменился"
        assert len(set(counts.values())) == 1, f"{field}: уровни неравномерны"


def test_order_is_shuffled_for_sharding():
    rows = tick_bot_rows()
    starts = [row["start_updown_ticks"] for row in rows]
    runs = sum(1 for a, b in zip(starts, starts[1:]) if a == b)

    assert runs < len(starts) / 2, f"порядок похож на неперемешанный: {runs}"


def main():
    test_grid_is_full_and_unchanged()
    test_levels_are_in_ticks_not_percents()
    test_single_pair()
    test_each_level_is_equally_represented()
    test_order_is_shuffled_for_sharding()
    print("tick seed: ок")


if __name__ == "__main__":
    main()
