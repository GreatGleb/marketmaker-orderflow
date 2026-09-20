"""Парк стратегии 1: сетка правил возврата к средней.

Что перебираем и почему именно это:

* `entry_offset_atr` — как далеко от цены стоит заявка. Главный
  параметр: слишком близко — ловим обычный шум и платим комиссию,
  слишком далеко — заявка не исполняется вовсе;
* `take_profit_fraction` — какую долю пройденного отступа забираем.
  Постановка называла 30–50%, но крайние значения тоже в сетке: на
  реальных возвратах оптимум может оказаться за её пределами;
* `stop_loss_atr` — где прекращаем спорить с рынком. Вынос, который не
  вернулся, — это начало движения, а не прострел;
* `time_stop_seconds` — сколько ждать возврата. Отдельно от стопа:
  позиция может не дойти ни до цели, ни до стопа и просто висеть;
* `side` — обе стороны, только лонги, только шорты. Дешёвая проверка
  того, нужен ли вообще фильтр тренда: если одна сторона стабильно
  хуже, его стоит строить, если нет — не стоит.

`atr_period` не перебирается: на минутных данных 14 — общепринятое
значение, и добавлять к сетке ещё одно измерение ради него значит
удвоить парк ради параметра, который меняет отступ на проценты.

Каждая конфигурация заводится **на каждой паре** набора, а не
раздаётся парам по кругу. Разница принципиальная: при раздаче по кругу
у конфигурации ровно один бот на одной паре, и её результат нельзя
отделить от характера этой пары — выигравший отступ может оказаться
свойством не отступа, а монеты. При полном кресте параметр сравнивается
при фиксированной паре, а пара — при фиксированном параметре.

Цена — размер парка: 243 конфигурации на 8 пар дают 1944 бота. Для
симулятора это немного (правило — 4–7 тысяч на процесс), а без креста
эксперимент не отвечает на вопрос, ради которого ставится.
"""
from decimal import Decimal
from itertools import product
from random import Random

from app.strategies.strategy_1.algorithm import SIDE_BOTH, SIDE_LONG, SIDE_SHORT

ENTRY_OFFSETS_ATR = (Decimal("1.5"), Decimal("2.5"), Decimal("3.5"))
TAKE_PROFIT_FRACTIONS = (Decimal("0.2"), Decimal("0.4"), Decimal("0.6"))
STOP_LOSS_ATR = (Decimal("1.5"), Decimal("2.5"), Decimal("4.0"))
TIME_STOPS_SECONDS = (15, 60, 180)
SIDES = (SIDE_BOTH, SIDE_LONG, SIDE_SHORT)

ATR_PERIOD = 14
MAX_WAIT_SECONDS = 300

# Ниже — не перебор, а условия эксперимента: они одинаковы у всех ботов,
# чтобы результаты оставались сопоставимыми. В конфиге они прописаны
# явно, а не оставлены умолчаниями алгоритма: умолчание поменяется — и
# уже заведённый парк молча сменит правила на ходу.
MIN_ATR_PERCENT = Decimal("0.05")
MAX_ATR_PERCENT = Decimal("10")
ATR_SPIKE_GUARD = Decimal("5")
# Проскальзывание рыночного выхода, в долях ATR. Не ноль: выход на паре,
# которую только что вынесло, исполняется хуже наблюдаемой цены, и
# идеальное закрытие завысило бы результат стратегии.
EXIT_SLIPPAGE_ATR = Decimal("0.1")

BALANCE = Decimal("1000.0")


def strategy_1_bot_rows(symbols) -> list[dict]:
    """Строки `test_bots` для парка стратегии 1.

    Без пар вернёт пустой список: бот без пары в симуляторе просто
    встанет, и заводить его незачем.
    """
    symbols = list(dict.fromkeys(symbols))

    if not symbols:
        return []

    grid = list(product(
        ENTRY_OFFSETS_ATR, TAKE_PROFIT_FRACTIONS, STOP_LOSS_ATR,
        TIME_STOPS_SECONDS, SIDES, symbols,
    ))

    rows = []

    for offset, take, stop, time_stop, side, symbol in grid:
        rows.append({
            "symbol": symbol,
            "balance": BALANCE,
            "is_active": True,
            "strategy_config": {
                "schema_version": 1,
                "atr_period": ATR_PERIOD,
                "entry_offset_atr": str(offset),
                "take_profit_fraction": str(take),
                "stop_loss_atr": str(stop),
                "time_stop_seconds": time_stop,
                "max_wait_seconds": MAX_WAIT_SECONDS,
                "side": side,
                "min_atr_percent": str(MIN_ATR_PERCENT),
                "max_atr_percent": str(MAX_ATR_PERCENT),
                "atr_spike_guard": str(ATR_SPIKE_GUARD),
                "exit_slippage_atr": str(EXIT_SLIPPAGE_ATR),
            },
        })

    # id распределяет ботов по шардам: порядок не должен повторять
    # порядок сетки, иначе в одном процессе окажутся соседние по
    # параметрам боты и нагрузка ляжет неравномерно.
    Random(0).shuffle(rows)

    return rows
