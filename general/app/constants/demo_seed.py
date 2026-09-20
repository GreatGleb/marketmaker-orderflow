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


# --- Тиковый парк: в сиде не участвует --------------------------------
#
# Сетка восстановлена из прежнего сида (до e002fe6) и оставлена как
# заготовка: сам парк выведен из эксперимента 2026-09-20.
#
# Почему выведен: уровни здесь в тиках, а тик у каждой пары свой,
# поэтому парк намертво привязан к одной паре — он стоял на BMTUSDT,
# которая по скринингу слабая (ATR 0.19%, 8 выносов в сутки). Те же
# 15 120 ботов давали три четверти всей нагрузки симулятора ради неё
# одной. Процентный парк делает то же самое — вход по пробою уровня, тот
# же алгоритм legacy, — но уровень в процентах сравним между парами, а
# пара выбирается по волатильности.
#
# Вернуть парк — добавить `tick_bot_rows()` в группы `create_bots`.
TICK_START_VALUES = (5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 100)
TICK_STOP_LOSS_VALUES = (20, 25, 30, 35, 40, 45, 50, 55, 60, 70, 80, 90, 150,
                         300, 450, 600, 850, 1000)
TICK_STOP_WIN_VALUES = (5, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 150, 300, 450)
TICK_TRAILING_VALUES = (False, True)
# Секунды ожидания цены входа. Два значения, а не одно: у MA-ботов этот
# параметр не работает вовсе (08-gotchas.md, пункт 5), у тиковых работает.
TICK_WAIT_SECONDS_VALUES = (1, 4)

# Пара тикового парка. BMTUSDT — та же, на которой он стоял до
# пересева: смена пары обнулила бы сопоставимость с накопленной
# статистикой, а она и есть главная ценность этого парка.
TICK_BOT_SYMBOL = "BMTUSDT"


def tick_bot_rows(symbol: str = TICK_BOT_SYMBOL) -> list[dict]:
    """Парк «по пробивам»: вход по движению на заданное число тиков."""
    rows = [
        {
            "symbol": symbol,
            "balance": Decimal("1000.0"),
            "start_updown_ticks": start,
            "stop_loss_ticks": stop_loss,
            "stop_success_ticks": stop_win,
            "use_trailing_stop": trailing,
            "time_to_wait_for_entry_price_to_open_order_in_seconds": wait,
            "is_active": True,
        }
        for start, stop_loss, stop_win, trailing, wait in product(
            TICK_START_VALUES, TICK_STOP_LOSS_VALUES, TICK_STOP_WIN_VALUES,
            TICK_TRAILING_VALUES, TICK_WAIT_SECONDS_VALUES,
        )
    ]

    # id распределяет ботов по шардам: порядок не должен повторять сетку,
    # иначе в одном процессе окажутся соседние по параметрам боты.
    Random(0).shuffle(rows)

    return rows


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
