"""Процентный парк: 2000 уникальных комбинаций из прежней сетки 2016."""

from decimal import Decimal
from itertools import product
from random import Random

from app.constants.copybot import copybot_v3_rows


def copybot_seed_groups() -> dict[str, list[dict]]:
    """Прежний полный набор копиботов; группы имеют разные колонки INSERT."""
    windows = (10, 20, 30, 40, 50, 60, 120, 180, 240, 360, 420, 480, 540,
               600, 660, 720, 1440, 1920, 2400, 2880)
    return {
        "v1": [
            {
                "symbol": "",
                "balance": Decimal("1000.0"),
                "copy_bot_min_time_profitability_min": window,
                "copybot_v1_check_for_24h_profitability": filter_24h,
                "copybot_v1_exclude_losing_donors": filter_ref,
            }
            for window, filter_24h, filter_ref in product(windows, (False, True), (False, True))
        ],
        "v2": [
            {
                "symbol": "",
                "balance": Decimal("1000.0"),
                "copybot_v2_time_in_minutes": window,
            }
            for window in windows
        ],
        "v3": copybot_v3_rows(),
    }


DEMO_BOT_COUNT = 2000
# Множители среднего процента за тик, а не тиковые параметры ботов.
START_MULTIPLIERS = (5, 10, 20, 30, 40, 50, 70, 100)
LOSS_MULTIPLIERS = (20, 30, 40, 50, 60, 80, 150, 300, 600)
WIN_MULTIPLIERS = (5, 10, 20, 40, 60, 100, 300)
VOLATILITY_MINUTES = (Decimal("0.5"), Decimal("1"), Decimal("2"), Decimal("3"))


def percentage_bot_rows(average_percent: Decimal) -> list[dict]:
    """Все уровни каждого параметра встречаются floor/ceil(2000 / levels) раз.

    Из 8 × 9 × 7 × 4 комбинаций исключаем 16, циклически проходя уровни
    каждой оси. Поэтому удалений на уровень поровну (либо разница 1).
    Это сохраняет весь диапазон и почти весь декартов продукт, в отличие
    от обрезания первых 2000 строк вложенных циклов.
    """
    if not average_percent.is_finite() or average_percent <= 0:
        raise ValueError("Средний процент за тик должен быть конечным и положительным")

    axes = (START_MULTIPLIERS, LOSS_MULTIPLIERS, WIN_MULTIPLIERS, VOLATILITY_MINUTES)
    grid = list(product(*axes))
    excluded = {
        tuple(axis[i % len(axis)] for axis in axes)
        for i in range(len(grid) - DEMO_BOT_COUNT)
    }
    rows = [
        {
            "symbol": "",
            "balance": Decimal("1000.0"),
            "start_updown_percents": start * average_percent,
            "stop_loss_percents": loss * average_percent,
            "stop_win_percents": win * average_percent,
            "min_timeframe_asset_volatility": timeframe,
            "is_active": True,
        }
        for start, loss, win, timeframe in grid
        if (start, loss, win, timeframe) not in excluded
    ]
    if len(rows) != DEMO_BOT_COUNT:
        raise ValueError("Сетка изменилась: необходимо пересчитать сбалансированную выборку")
    # id распределяет ботов по шардам: порядок не должен повторять окна.
    Random(0).shuffle(rows)
    return rows
