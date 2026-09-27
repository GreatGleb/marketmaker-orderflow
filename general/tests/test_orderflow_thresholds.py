"""Счётчик порогов прострела: сетка, события, направление, издержки.

Голые assert, без pytest. Сеть и база не нужны — всё считается на
искусственных сутках.

Эти проверки стерегут выводы раздела 9 плана: ошибка в знаке или в
forward-fill не уронит скрипт, а поменяет ответ на противоположный и
сделает это молча.
"""
import numpy as np
import pandas as pd

from app.scripts.orderflow_thresholds import (
    BUCKETS_PER_DAY,
    BUCKETS_PER_SECOND,
    COST_ROUND_TRIP_PERCENT,
    ENTRY_LAG_BUCKETS,
    EVENT_COOLDOWN_SECONDS,
    day_grid,
    day_start_ms,
    measure,
    rising_edges,
)

DAY_MS = 1_000_000_000_000


def trades(rows) -> pd.DataFrame:
    """rows: (смещение в мс, цена, объём, is_buyer_maker)."""
    return pd.DataFrame(
        [(DAY_MS + ms, price, qty, maker) for ms, price, qty, maker in rows],
        columns=["transact_time", "price", "quantity", "is_buyer_maker"],
    ).astype({"transact_time": np.int64, "is_buyer_maker": bool})


def test_grid_forward_fills_and_splits_sides():
    """Такт без сделок повторяет последнюю цену, а не обнуляется.

    Бот в таком такте видит именно её: питатель держит в Redis последнее
    значение. Пропуск вместо цены превратил бы тихую пару в сплошные
    несработавшие триггеры.
    """
    frame = trades([
        (0, 100.0, 5.0, False),    # покупка
        (250, 101.0, 3.0, True),   # продажа
        (1000, 102.0, 2.0, False),
    ])
    prices, buys, sells = day_grid(frame, DAY_MS)

    assert prices.size == BUCKETS_PER_DAY
    assert prices[0] == 100.0
    # такты 1 и 2 пустые — цена держится на 100 до сделки в такте 2
    assert prices[1] == 100.0
    assert prices[2] == 101.0
    assert prices[3] == 101.0
    assert prices[10] == 102.0

    # is_buyer_maker=True — агрессором был продавец.
    assert buys[0] == 5.0 and sells[0] == 0.0
    assert sells[2] == 3.0 and buys[2] == 0.0
    assert buys[10] == 2.0


def test_grid_leaves_start_of_day_unknown():
    """До первой сделки суток цены нет, и выдумывать её нельзя."""
    frame = trades([(5000, 50.0, 1.0, False)])
    prices, _, _ = day_grid(frame, DAY_MS)

    assert np.isnan(prices[:50]).all()
    assert prices[50] == 50.0


def test_grid_drops_trades_outside_the_day():
    """Соседние сутки в файл иногда попадают; в сетку они не идут."""
    frame = trades([(-5, 1.0, 1.0, False), (0, 2.0, 1.0, False)])
    prices, _, _ = day_grid(frame, DAY_MS)

    assert prices[0] == 2.0


def test_rising_edges_collapses_runs():
    """Подряд идущие такты одного движения — одно событие, а не сотня.

    Бот входит один раз и дальше держит позицию. Без схлопывания одно
    движение дало бы сотни «наблюдений», и любая значимость оказалась бы
    нарисованной.
    """
    fired = np.zeros(10_000, dtype=bool)
    fired[100:400] = True

    cooldown = EVENT_COOLDOWN_SECONDS * BUCKETS_PER_SECOND
    assert rising_edges(fired, cooldown).tolist() == [100]

    # Второе движение дальше паузы — отдельное событие.
    fired[100 + cooldown + 5] = True
    assert rising_edges(fired, cooldown).tolist() == [100, 100 + cooldown + 5]

    # Ровно на границе паузы событие ещё считается.
    assert rising_edges(fired, 50).tolist()[:2] == [100, 150]
    assert rising_edges(np.zeros(10, dtype=bool), 5).size == 0


def test_measure_finds_the_move_and_signs_both_modes():
    """Прострел вверх: continuation покупает, reversal продаёт.

    Проверяется именно знак: перепутанный, он превратит убыточное
    правило в прибыльное на бумаге и обнулит весь раздел 9.
    """
    prices = np.full(BUCKETS_PER_DAY, 100.0)
    trigger = 10 * BUCKETS_PER_SECOND
    # Движение +2% за один такт, дальше цена стоит.
    prices[trigger:] = 102.0

    buys = np.zeros(BUCKETS_PER_DAY)
    sells = np.zeros(BUCKETS_PER_DAY)
    rows = measure(prices, buys, sells, window_seconds=1, move_percent=2.0)

    assert rows, "движение 2% обязано сработать при пороге 2%"

    by_mode = {}
    for row in rows:
        if row["horizon_seconds"] == 30:
            by_mode[row["entry_mode"]] = row

    # Цена после входа не меняется, значит валовый результат нулевой,
    # а чистый — ровно минус издержки, с обеих сторон.
    assert by_mode["continuation"]["gross"][0] == 0.0
    assert by_mode["reversal"]["gross"][0] == 0.0
    assert by_mode["continuation"]["move_direction"][0] == 1.0
    assert by_mode["reversal"]["move_direction"][0] == 1.0

    # Вход берётся с задержкой: цена на такте входа уже 102, а не 100.
    assert prices[trigger + ENTRY_LAG_BUCKETS] == 102.0
    assert by_mode["continuation"]["events"] == 1


def test_measure_direction_of_profit():
    """Разворот после выброса: reversal в плюсе, continuation в минусе."""
    prices = np.full(BUCKETS_PER_DAY, 100.0)
    trigger = 10 * BUCKETS_PER_SECOND
    prices[trigger:] = 102.0
    # Через 30 секунд цена вернулась обратно.
    back = trigger + ENTRY_LAG_BUCKETS + 30 * BUCKETS_PER_SECOND
    prices[back:] = 100.0

    rows = measure(prices, np.zeros(BUCKETS_PER_DAY),
                   np.zeros(BUCKETS_PER_DAY), 1, 2.0)
    picked = {r["entry_mode"]: r for r in rows if r["horizon_seconds"] == 30}

    assert picked["reversal"]["gross"][0] > 0
    assert picked["continuation"]["gross"][0] < 0
    # Зеркальность: одна и та же сделка с разных сторон.
    assert np.isclose(
        picked["reversal"]["gross"][0], -picked["continuation"]["gross"][0]
    )


def test_measure_ignores_moves_below_threshold():
    prices = np.full(BUCKETS_PER_DAY, 100.0)
    prices[10 * BUCKETS_PER_SECOND:] = 100.5

    assert measure(prices, np.zeros(BUCKETS_PER_DAY),
                   np.zeros(BUCKETS_PER_DAY), 1, 2.0) == []


def test_cost_barrier_matches_our_rates():
    """Барьер считается по ставкам USDⓈ-M, а не по спотовым.

    0.1% комиссии за круг плюс 0.0264% спреда. Спотовые 0.2% дали бы
    барьер вдвое выше и отбросили бы нижние пороги движения как заведомо
    негодные — ровно та ошибка, от которой предостерегает раздел 5.1.
    """
    assert abs(COST_ROUND_TRIP_PERCENT - 0.1264) < 1e-9


def test_day_start_parsed_from_name():
    path = type("P", (), {"stem": "ALGOUSDT-aggTrades-2026-08-31"})()

    # 2026-08-31 00:00 UTC; следующие сутки начинаются на 86 400 000 мс позже.
    assert day_start_ms(path) == 1788134400000


if __name__ == "__main__":
    test_grid_forward_fills_and_splits_sides()
    test_grid_leaves_start_of_day_unknown()
    test_grid_drops_trades_outside_the_day()
    test_rising_edges_collapses_runs()
    test_measure_finds_the_move_and_signs_both_modes()
    test_measure_direction_of_profit()
    test_measure_ignores_moves_below_threshold()
    test_cost_barrier_matches_our_rates()
    test_day_start_parsed_from_name()
    print("Проверки счётчика порогов прошли.")
