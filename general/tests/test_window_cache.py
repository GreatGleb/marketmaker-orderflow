"""Как часто пересчитываются окна разной длины."""
import asyncio
import time
from decimal import Decimal
from unittest.mock import patch

from app.workers.profitable_bot_updater import (
    ProfitableBotUpdaterCommand as P,
    WindowCache,
)


class CountingCrud:
    def __init__(self):
        self.calls = []

    async def get_sorted_by_profit(self, since, **kw):
        self.calls.append(since.total_seconds() / 60)
        return [(101, Decimal("500"), 10, 8)]


def show_lifetimes():
    cache = WindowCache()
    print("  срок годности рейтинга по длине окна:")

    for minutes in (10, 60, 720, 2880):
        seconds = cache.lifetime_seconds(minutes)
        print(f"    окно {minutes:>5} мин → пересчёт раз в {seconds / 60:6.1f} мин")

    assert cache.lifetime_seconds(10) == 30, "короткое окно должно жить 30 с"
    assert cache.lifetime_seconds(2880) == 8640


async def main():
    show_lifetimes()

    print("\n  30 циклов воркера по 30 секунд (виртуальных):")
    cache = WindowCache()
    crud = CountingCrud()
    params = {
        1: {"tf": 10, "24h": False, "no_losing_donors": False},
        2: {"tf": 720, "24h": False, "no_losing_donors": False},
        3: {"tf": 2880, "24h": False, "no_losing_donors": False},
    }

    # Часы с нуля: у настоящего monotonic значение порядка миллионов, и
    # прибавление 30 секунд там теряет точность ровно на границе срока
    # годности. Тест мерил бы float, а не поведение.
    now = [0.0]

    with patch.object(time, "monotonic", lambda: now[0]):
        for _ in range(30):
            await P.get_profitable_bots_id_by_individual_params(
                bot_crud=crud, bot_profitability_parameters=params,
                window_cache=cache,
            )
            now[0] += 30

    by_window = {}

    for minutes in crud.calls:
        by_window[minutes] = by_window.get(minutes, 0) + 1

    for minutes in sorted(by_window):
        print(f"    окно {minutes:>6.0f} мин → пересчитано "
              f"{by_window[minutes]:>2} раз из 30")

    assert by_window[10] == 30, "короткое окно должно считаться каждый цикл"
    assert by_window[720] <= 2, by_window[720]
    assert by_window[2880] == 1, by_window[2880]

    total_without = 30 * len(params)
    total_with = len(crud.calls)
    print(f"\n  запросов: было бы {total_without}, стало {total_with} "
          f"— в {total_without / total_with:.1f} раза меньше")

    print("\nOK: длинные окна не пересчитываются впустую")


if __name__ == "__main__":
    asyncio.run(main())
