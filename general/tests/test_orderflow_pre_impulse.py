"""Признаки до импульса: окно [T0 − 500 мс, T0) и ничего из самого T0.

Голые assert, без pytest. Сеть, база и архив не нужны.

Стерегут то, ради чего скрипт написан: признак берётся **до** движения.
Снимок книги с `E == T0`, сделка ровно в T0, T0 на такт позже — любая из
этих ошибок пустит в признак начало импульса, и OBI с CVD «предскажут»
движение, которое уже идёт (раздел 9.4 плана: так дельта совпадала со
знаком движения в 97% случаев).
"""
import numpy as np
import pandas as pd

from app.scripts.impulse_express import BUCKETS_PER_DAY, GRID_MS
from app.scripts.orderflow_pre_impulse import (
    BASELINE_SEC,
    CONTROL_EXCLUSION_SEC,
    MAX_BOOK_AGE_MS,
    PRE_WINDOW_MS,
    auc,
    book_features,
    control_buckets,
    impulse_start_ms,
    trade_features,
    trade_sums,
)


def test_impulse_start_is_end_of_base_bucket():
    # Окно 2 с = 20 тактов: срабатывание в такте 100 сравнивает с концом
    # такта 80, то есть движение правее отметки 81 × 100 мс.
    assert impulse_start_ms(0, 100, 2.0) == 81 * GRID_MS
    assert impulse_start_ms(1_000, 100, 1.0) == 1_000 + 91 * GRID_MS


def book(times, obi5):
    return pd.DataFrame({
        "time": times,
        "obi_l5": obi5,
        "obi_l10": obi5,
        "spread_bps": [1.0] * len(times),
    })


def test_book_snapshot_at_t0_is_excluded():
    frame = book([9_000, 10_000], [0.1, 0.9])
    features = book_features(frame, 10_000)
    assert features["obi_l5"] == 0.1
    assert features["book_age_ms"] == 1_000


def test_book_delta_and_same_snapshot_flag():
    frame = book([9_000, 9_700], [0.1, 0.4])
    features = book_features(frame, 10_000)
    # На начало окна (9 500) действовал снимок 9 000, на конец — 9 700.
    assert abs(features["obi_l5_delta"] - 0.3) < 1e-12
    assert features["book_same"] == 0.0

    features = book_features(book([9_000], [0.2]), 9_800)
    assert features["book_same"] == 1.0
    assert features["obi_l5_delta"] == 0.0


def test_stale_book_is_empty():
    frame = book([1_000], [0.5])
    features = book_features(frame, 1_000 + MAX_BOOK_AGE_MS + 1)
    assert np.isnan(features["obi_l5"])
    assert np.isnan(book_features(None, 5_000)["obi_l10"])


def test_trades_window_is_half_open():
    t0 = BASELINE_SEC * 1000 + 10_000
    trades = pd.DataFrame({
        # до окна (база), в окне дважды, ровно в T0 — не в окне.
        "time": [t0 - 5_000, t0 - PRE_WINDOW_MS, t0 - 1, t0],
        "quote": [100.0, 30.0, 10.0, 1_000.0],
        "sign": [1.0, 1.0, -1.0, 1.0],
    })
    features = trade_features(trades, trade_sums(trades), t0)
    assert features["trades"] == 2
    assert features["cvd_quote"] == 20.0
    assert abs(features["cvd_share"] - 0.5) < 1e-12
    # База — 100 USDT за 299.5 с = 599 окон по 500 мс.
    expected = 40.0 / (100.0 / 599)
    assert abs(features["volume_burst"] - expected) < 1e-9


def test_controls_keep_distance_from_events():
    rng = np.random.default_rng(0)
    event = BUCKETS_PER_DAY // 2
    picks = control_buckets(np.array([event]), 100, 5_000, rng)
    radius = CONTROL_EXCLUSION_SEC * 10
    assert picks.size == 5_000
    assert np.all(np.abs(picks - event) >= radius)
    assert picks.min() >= BASELINE_SEC * 10


def test_auc_extremes():
    assert auc(np.array([2.0, 3.0]), np.array([0.0, 1.0])) == 1.0
    assert auc(np.array([0.0]), np.array([1.0, np.nan])) == 0.0
    assert auc(np.array([1.0]), np.array([1.0])) == 0.5


def main():
    test_impulse_start_is_end_of_base_bucket()
    test_book_snapshot_at_t0_is_excluded()
    test_book_delta_and_same_snapshot_flag()
    test_stale_book_is_empty()
    test_trades_window_is_half_open()
    test_controls_keep_distance_from_events()
    test_auc_extremes()
    print("Проверки признаков до импульса прошли.")


if __name__ == "__main__":
    main()
