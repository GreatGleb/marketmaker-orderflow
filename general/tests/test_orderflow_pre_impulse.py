"""Признаки до импульса: окно [T0 − 500 мс, T0) и ничего из самого T0.

Голые assert, без pytest. Сеть, база и архив не нужны.

Стерегут то, ради чего скрипт написан: признак берётся **до** движения.
Снимок книги с `E == T0`, сделка ровно в T0, T0 на такт позже — любая из
этих ошибок пустит в признак начало импульса, и OBI с CVD «предскажут»
движение, которое уже идёт (раздел 9.4 плана: так дельта совпадала со
знаком движения в 97% случаев).
"""
import gzip
import tempfile
from pathlib import Path

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
    calm_mask,
    cluster_diff,
    control_buckets,
    load_book_tardis,
    t_two_sided_p,
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


def test_t_p_value_matches_tables():
    # Табличные квантили: t(23) 0.975 = 2.0687, t(23) 0.995 = 2.8073.
    assert abs(t_two_sided_p(2.0687, 23) - 0.05) < 1e-3
    assert abs(t_two_sided_p(2.8073, 23) - 0.01) < 1e-3
    assert abs(t_two_sided_p(-2.0687, 23) - 0.05) < 1e-3
    assert t_two_sided_p(0.0, 23) == 1.0
    assert abs(t_two_sided_p(1.96, 10_000) - 0.05) < 1e-3


def test_cluster_diff_is_difference_of_means():
    rng = np.random.default_rng(1)
    days = np.repeat(np.arange(24), 50)
    is_event = np.tile(np.r_[np.ones(10), np.zeros(40)], 24).astype(bool)
    values = rng.normal(0, 1, days.size) + 0.5 * is_event
    out = cluster_diff(values, is_event, days)
    expected = values[is_event].mean() - values[~is_event].mean()
    assert abs(out["разность"] - expected) < 1e-12
    assert out["кластеров"] == 24
    assert out["t"] > 3 and out["p"] < 0.01


def test_cluster_diff_sees_day_effect():
    # Сдвиг, общий для всего дня, у событий и контроля одинаков — t около
    # нуля. Сдвиг только у событий одного дня — не значим при 24 днях.
    days = np.repeat(np.arange(24), 20)
    is_event = np.tile(np.r_[np.ones(5), np.zeros(15)], 24).astype(bool)
    values = np.where(is_event & (days == 0), 10.0, 0.0) + 0.01 * np.arange(
        days.size) % 1
    out = cluster_diff(values, is_event, days)
    assert out["p"] > 0.1


def test_tardis_book_is_parsed_like_own():
    header = ["exchange", "symbol", "timestamp", "local_timestamp"]
    for i in range(25):
        header += [f"asks[{i}].price", f"asks[{i}].amount",
                   f"bids[{i}].price", f"bids[{i}].amount"]
    row = ["binance-futures", "X", "1000400", "1000500"]
    for i in range(25):
        # bid 3 на уровень, ask 1: OBI = (3 − 1) / 4 = 0.5 на любой глубине.
        row += [str(100.1 + i), "1", str(99.9 - i), "3"]
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "X.csv.gz"
        with gzip.open(path, "wt") as handle:
            handle.write(",".join(header) + "\n" + ",".join(row) + "\n")
        book = load_book_tardis(path)
    # 1 000 400 мкс -> 1000 мс, отброшенная дробь.
    assert book["time"].tolist() == [1000]
    assert abs(book["obi_l5"].iat[0] - 0.5) < 1e-12
    assert abs(book["obi_l10"].iat[0] - 0.5) < 1e-12
    assert abs(book["spread_bps"].iat[0] - 0.2 / 100 * 1e4) < 1e-9
    # Снимок в 1000 мс для T0 = 1000 — в будущем, для T0 = 1001 — годен.
    assert np.isnan(book_features(book, 1000, 600_000)["obi_l5"])
    assert book_features(book, 1001, 600_000)["obi_l5"] == 0.5


def test_calm_mask_excludes_moves_before_and_after():
    change = np.zeros(100)
    change[50] = 1.0
    calm = calm_mask(change, 20, 0.5)
    # Окно, кончающееся в 50, и окно после 30 (кончается в 50) — не спокойны.
    assert not calm[50] and not calm[30]
    assert calm[29] and calm[51]
    assert not calm[-1]


def main():
    test_impulse_start_is_end_of_base_bucket()
    test_book_snapshot_at_t0_is_excluded()
    test_book_delta_and_same_snapshot_flag()
    test_stale_book_is_empty()
    test_trades_window_is_half_open()
    test_controls_keep_distance_from_events()
    test_auc_extremes()
    test_t_p_value_matches_tables()
    test_cluster_diff_is_difference_of_means()
    test_cluster_diff_sees_day_effect()
    test_tardis_book_is_parsed_like_own()
    test_calm_mask_excludes_moves_before_and_after()
    print("Проверки признаков до импульса прошли.")


if __name__ == "__main__":
    main()
