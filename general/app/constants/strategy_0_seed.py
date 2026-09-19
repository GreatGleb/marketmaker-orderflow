"""Парк стратегии 0: сетка правил прострела.

Что перебираем и почему именно это:

* `window_seconds` — за сколько набрано движение. Две секунды и
  полминуты — разные явления, и какое из них торгуемо, заранее
  неизвестно;
* `move_percent` — что считать прострелом. Нижняя граница выбрана так,
  чтобы движение было заметно крупнее комиссии в обе стороны;
* `entry_mode` — входить по движению или против него. Это главная
  развилка стратегии, и обе ветки должны попасть в перебор;
* `take_profit_percent` / `stop_loss_percent` — цель и стоп, оба в
  процентах от цены входа;
* `max_hold_seconds` — сколько держать, если ни один уровень не задет.

Пара берётся из набора стратегии и раздаётся конфигурациям по кругу:
иначе весь парк встанет на одну пару, и перебор правил превратится в
перебор её характера.
"""
from decimal import Decimal
from itertools import product
from random import Random

from app.strategies.strategy_0.algorithm import (
    ENTRY_CONTINUATION,
    ENTRY_REVERSAL,
)

WINDOW_SECONDS = (2, 5, 10, 30)
MOVE_PERCENTS = (Decimal("0.3"), Decimal("0.5"), Decimal("1.0"))
ENTRY_MODES = (ENTRY_CONTINUATION, ENTRY_REVERSAL)
TAKE_PERCENTS = (Decimal("0.2"), Decimal("0.4"), Decimal("0.8"))
STOP_PERCENTS = (Decimal("0.2"), Decimal("0.4"), Decimal("0.8"))
MAX_HOLD_SECONDS = (30, 60, 180)

BALANCE = Decimal("1000.0")


def strategy_0_bot_rows(symbols) -> list[dict]:
    """Строки `test_bots` для парка стратегии 0.

    Без пар вернёт пустой список: бот без пары в симуляторе просто
    встанет, и заводить его незачем.
    """
    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        return []

    grid = list(product(
        WINDOW_SECONDS, MOVE_PERCENTS, ENTRY_MODES,
        TAKE_PERCENTS, STOP_PERCENTS, MAX_HOLD_SECONDS,
    ))

    rows = []

    for index, (window, move, mode, take, stop, hold) in enumerate(grid):
        rows.append({
            "symbol": symbols[index % len(symbols)],
            "balance": BALANCE,
            "is_active": True,
            "strategy_config": {
                "schema_version": 1,
                "window_seconds": window,
                "move_percent": str(move),
                "entry_mode": mode,
                "take_profit_percent": str(take),
                "stop_loss_percent": str(stop),
                "max_hold_seconds": hold,
            },
        })

    # id распределяет ботов по шардам: порядок не должен повторять
    # порядок сетки, иначе в одном процессе окажутся соседние по
    # параметрам боты и нагрузка ляжет неравномерно.
    Random(0).shuffle(rows)

    return rows
