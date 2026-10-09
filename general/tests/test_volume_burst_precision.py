"""Точность всплеска объёма: окна, прореживание, горизонт, кластерное среднее.

Голые assert, без pytest. Сеть, база и архив не нужны.

Стерегут то, что сдвигает результат молча: окно признака, заглянувшее в
текущий такт; «сквиз впереди», засчитанный в самом такте сигнала;
прореживание, пропускающее сигналы; t по дням, посчитанная по сделкам.
"""
import numpy as np

from app.scripts.impulse_express import first_crossings
from app.scripts.volume_burst_precision import (
    any_ahead,
    clustered_mean,
    debounce,
    window_sum,
)


def test_window_sum_excludes_current_bucket():
    values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = window_sum(values, 2)
    assert np.isnan(out[0]) and np.isnan(out[1])
    # out[b] = values[b−2] + values[b−1]; сам такт b не входит.
    assert out[2] == 3.0 and out[3] == 5.0 and out[4] == 7.0


def test_debounce_matches_first_crossings():
    rng = np.random.default_rng(3)
    flags = rng.random(20_000) < 0.02
    fast = debounce(np.flatnonzero(flags), 300)
    slow = first_crossings(flags, 300)
    assert np.array_equal(fast, slow)


def test_any_ahead_skips_own_bucket():
    flags = np.zeros(10, dtype=bool)
    flags[5] = True
    ahead = any_ahead(flags, 3)
    # Такты 2, 3, 4 видят сквиз в 5; сам такт 5 — нет.
    assert ahead.tolist() == [False, False, True, True, True,
                              False, False, False, False, False]


def test_clustered_mean_uses_day_residuals():
    # Два дня: среднее по наблюдениям, а не по дням.
    mean, se, groups = clustered_mean(np.array([10.0, 0.0]), np.array([10, 30]))
    assert abs(mean - 0.25) < 1e-12 and groups == 2
    # Одинаковое среднее во все дни — остатков нет, ошибка ноль.
    mean, se, groups = clustered_mean(np.array([2.0, 4.0, 6.0]),
                                      np.array([1, 2, 3]))
    assert abs(mean - 2.0) < 1e-12 and se == 0.0
    # Пустые дни не считаются кластерами.
    _, _, groups = clustered_mean(np.array([1.0, 0.0, 3.0]), np.array([1, 0, 1]))
    assert groups == 2


def main():
    test_window_sum_excludes_current_bucket()
    test_debounce_matches_first_crossings()
    test_any_ahead_skips_own_bucket()
    test_clustered_mean_uses_day_residuals()
    print("Проверки точности всплеска объёма прошли.")


if __name__ == "__main__":
    main()
