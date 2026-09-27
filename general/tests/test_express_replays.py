"""Экспресс-скрипты: TP/SL по пути цены и реплей стратегии 1.

Голые assert, без pytest. Сеть, база и архив не нужны — всё считается на
искусственных сутках.

Стерегут знак и порядок проверок в `impulse_express.simulate` (на нём же
стоит `squeeze_express`) и правила `strategy1_calibrate.replay`:
пробитие вместо касания, цена заявки как цена входа, стоп раньше цели,
проскальзывание выхода против позиции. Ошибка в любом из них скрипт не
уронит, а тихо сдвинет матожидание — на числах, по которым решают,
заводить ли парк.
"""
import numpy as np

from app.scripts.impulse_express import BUCKETS_PER_DAY, simulate
from app.scripts.strategy1_calibrate import (
    EXIT_SLIPPAGE_ATR,
    levels,
    replay,
)


def flat_day(price=100.0):
    return np.full(BUCKETS_PER_DAY, price)


def test_simulate_long_take_and_short_mirror():
    last = flat_day()
    last[5:] = 100.6
    high, low = last.copy(), last.copy()

    gross, outcome = simulate(100.0, 1, 0.5, 0.3, high, low, last, 1, 100)
    assert outcome == "tp" and gross == 0.5

    # Шорт на том же пути — стоп: цена ушла против позиции.
    gross, outcome = simulate(100.0, -1, 0.5, 0.3, high, low, last, 1, 100)
    assert outcome == "sl" and gross == -0.3


def test_simulate_both_levels_in_one_bucket_is_stop():
    """Порядок внутри такта неизвестен — засчитывается худший исход."""
    last = flat_day()
    high, low = last.copy(), last.copy()
    high[3], low[3] = 101.0, 99.0

    gross, outcome = simulate(100.0, 1, 0.5, 0.3, high, low, last, 1, 100)
    assert outcome == "sl" and gross == -0.3


def test_simulate_timeout_takes_last_price():
    last = flat_day()
    last[50:] = 100.1
    high, low = last.copy(), last.copy()

    gross, outcome = simulate(
        100.0, -1, np.inf, np.inf, high, low, last, 1, 100,
    )
    assert outcome == "timeout"
    assert abs(gross - (-0.1)) < 1e-9


def replay_day(last, atr_percent, config, tick=0.01):
    atr = np.full(BUCKETS_PER_DAY, atr_percent)
    return replay(last, atr, tick, config, levels(last, atr, tick, config[0]))


def test_replay_touch_is_not_a_fill():
    """Цена дошла ровно до заявки и вернулась — сделки нет.

    Провал стоит внутри блока пересчёта (50 тактов), а не на его
    границе: пересчёт на самом дне поставил бы базу на 99.0, и возврат к
    100 пробил бы уже заявку на продажу. Так ведёт себя и бот.
    """
    last = flat_day()
    # ATR 1%, отступ 1 ATR: заявка на покупку на 99.0.
    last[120:126] = 99.0

    assert replay_day(last, 1.0, (1.0, 0.5, 2.0, 15)) == []


def test_replay_pierce_fills_at_order_price_and_takes_profit():
    last = flat_day()
    last[120:126] = 98.9        # пробитие 99.0 больше чем на тик
    last[126:] = 99.6           # возврат выше цели 99.0 + 0.5

    trades = replay_day(last, 1.0, (1.0, 0.5, 2.0, 15))
    side, gross, _ = trades[0]

    assert side == 1
    # Вход по 99.0 (цена заявки, а не 98.9), выход по такту, где задета
    # цель, минус проскальзывание 0.1 ATR от входа.
    slip = 99.0 * 1.0 * EXIT_SLIPPAGE_ATR / 100
    expected = (99.6 - slip - 99.0) / 99.0 * 100
    assert abs(gross - expected) < 1e-9


def test_replay_stop_before_take_and_short_side():
    last = flat_day()
    last[120:126] = 101.1       # пробитие продажи 101.0
    last[126:] = 103.5          # стоп 2 ATR от входа: 101.0 + 2.02

    trades = replay_day(last, 1.0, (1.0, 0.5, 2.0, 15))
    side, gross, _ = trades[0]

    assert side == -1
    assert gross < -2.0


def test_replay_no_levels_without_atr():
    """Нет ATR (фильтр отбраковал) — заявок нет, а не заявка вплотную."""
    last = flat_day()
    last[120:126] = 90.0

    assert replay_day(last, np.nan, (1.0, 0.5, 2.0, 15)) == []


def main():
    test_simulate_long_take_and_short_mirror()
    test_simulate_both_levels_in_one_bucket_is_stop()
    test_simulate_timeout_takes_last_price()
    test_replay_touch_is_not_a_fill()
    test_replay_pierce_fills_at_order_price_and_takes_profit()
    test_replay_stop_before_take_and_short_side()
    test_replay_no_levels_without_atr()
    print("Проверки экспресс-скриптов прошли.")


if __name__ == "__main__":
    main()
