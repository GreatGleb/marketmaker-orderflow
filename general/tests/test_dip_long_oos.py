"""Лонг провала по портфелю: допуск по объёму, кулдаун, лимит позиций.

Голые assert, без pytest. Сеть, база и архив не нужны.

Стерегут то, что сдвигает результат молча: объём дня сигнала вместо
прошлых суток (подглядывание), кулдаун от пропущенного сигнала, третью
позицию сверх лимита и случайный порядок пар внутри одного такта.
"""
import numpy as np
import pandas as pd

from app.scripts.dip_long_oos import (
    COOLDOWN_BUCKETS,
    ENTRY_LAG_BUCKETS,
    HOLD_BUCKETS,
    MIN_QUOTE_VOLUME_USD,
    clustered_stats,
    eligible_pairs,
    select_trades,
    t_critical_975,
)


def candidates(rows):
    return pd.DataFrame(rows, columns=["symbol", "day", "bucket", "prev_volume"])


def test_eligibility_uses_previous_day():
    big = MIN_QUOTE_VOLUME_USD * 2
    volumes = pd.DataFrame(
        [("AUSDT", "2026-06-01", big), ("BUSDT", "2026-06-01", 1.0),
         ("BUSDT", "2026-06-02", big)],
        columns=["symbol", "day", "quote_volume"],
    )
    out = eligible_pairs(volumes)
    # Объём 1 июня допускает пару ко 2 июня, а не к 1-му.
    assert set(map(tuple, out[["symbol", "day"]].to_numpy())) == {
        ("AUSDT", "2026-06-02"), ("BUSDT", "2026-06-03"),
    }


def test_cooldown_and_limit():
    occupied = ENTRY_LAG_BUCKETS + HOLD_BUCKETS
    day = "2026-06-01"
    frame = candidates([
        ("A", day, 100, 3.0),
        ("A", day, 101, 3.0),                      # кулдаун пары
        ("B", day, 100, 2.0),
        ("C", day, 100, 1.0),                      # третья позиция
        ("C", day, 100 + occupied - 1, 1.0),       # слоты ещё заняты
        ("C", day, 100 + occupied, 1.0),           # слот освободился
        ("A", day, 100 + COOLDOWN_BUCKETS, 3.0),   # кулдаун прошёл, слоты заняты
        ("A", day, 100 + occupied, 3.0),
    ])
    taken = select_trades(frame)
    got = list(zip(taken["symbol"], taken["bucket"]))
    # Пропуск C из-за лимита кулдаун не запускает. На такте освобождения
    # оба слота свободны, ликвидная A идёт первой, C — второй.
    assert got == [
        ("A", 100), ("B", 100),
        ("A", 100 + occupied), ("C", 100 + occupied),
    ], got


def test_tie_break_by_volume_not_name():
    day = "2026-06-01"
    frame = candidates([
        ("AAA", day, 5, 1.0), ("ZZZ", day, 5, 9.0), ("MMM", day, 5, 5.0),
    ])
    assert list(select_trades(frame)["symbol"]) == ["ZZZ", "MMM"]


def test_clustered_stats_counts_days():
    values = np.array([1.0, 1.0, 1.0, -1.0])
    days = np.array(["d1", "d1", "d1", "d2"])
    mean, error, t, low, high = clustered_stats(values, days)
    # Остатки по дням: +1.5 и −1.5; CR1: 2/1 · 4.5 / 16.
    assert mean == 0.5
    assert abs(error - np.sqrt(2 * 4.5 / 16)) < 1e-12
    assert abs(t - mean / error) < 1e-12 and low < mean < high


def test_t_critical():
    # Табличные значения t(0.975).
    for df, exact in ((20, 2.0860), (30, 2.0423), (120, 1.9799)):
        assert abs(t_critical_975(df) - exact) < 1e-3, df


def main():
    test_eligibility_uses_previous_day()
    test_cooldown_and_limit()
    test_tie_break_by_volume_not_name()
    test_clustered_stats_counts_days()
    test_t_critical()
    print("Проверки лонга провала прошли.")


if __name__ == "__main__":
    main()
