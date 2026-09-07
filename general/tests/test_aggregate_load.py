"""Во сколько обходится воркеру копиботов час работы.

Считает не только запросы, но и объём просканированных строк: он и есть
настоящая нагрузка на Postgres. Оценка по 60 сделкам в секунду — столько даёт
парк из 2 116 ботов.
"""
import asyncio
import time
from decimal import Decimal
from unittest.mock import patch

from app.workers.profitable_bot_updater import (
    ProfitableBotUpdaterCommand as P,
    WindowCache,
)

WINDOWS = [10, 20, 30, 40, 50, 60, 120, 180, 240, 360, 420, 480, 540,
           600, 660, 720, 1440, 1920, 2400, 2880]
TRADES_PER_SECOND = 60
CYCLE_SECONDS = 30
HOURS = 1


class CountingCrud:
    def __init__(self):
        self.scanned_minutes = 0
        self.calls = 0

    async def get_sorted_by_profit(self, since, **kw):
        self.calls += 1
        self.scanned_minutes += since.total_seconds() / 60
        return [(101, Decimal("500"), 10, 8)]


def build_params():
    params, bot_id = {}, 1

    for window in WINDOWS:
        for check_24h in (False, True):
            for by_ref in (False, True):
                params[bot_id] = {"tf": window, "24h": check_24h,
                                  "by_ref": by_ref}
                bot_id += 1

    return params


def as_rows(minutes):
    return minutes * 60 * TRADES_PER_SECOND


async def run(cache_between_cycles: bool, cycles: int, params):
    crud = CountingCrud()
    shared = WindowCache() if cache_between_cycles else None
    now = [0.0]

    with patch.object(time, "monotonic", lambda: now[0]):
        for _ in range(cycles):
            await P.get_profitable_bots_id_by_individual_params(
                bot_crud=crud, bot_profitability_parameters=params,
                window_cache=shared,
            )
            now[0] += CYCLE_SECONDS

    return crud


async def main():
    params = build_params()
    cycles = HOURS * 3600 // CYCLE_SECONDS

    print(f"  {len(params)} копиботов, {cycles} циклов = {HOURS} ч работы")
    print(f"  {TRADES_PER_SECOND} сделок в секунду\n")

    # Как было до правок: ни дедупликации, ни сроков годности.
    naive_calls = cycles * (len(WINDOWS) * 4 + len(WINDOWS) * 2 + len(WINDOWS) * 2)
    naive_minutes = cycles * (
        sum(WINDOWS) * 4 + 1440 * len(WINDOWS) * 2 + sum(WINDOWS) * 2
    )

    per_cycle = await run(cache_between_cycles=False, cycles=1, params=params)
    full = await run(cache_between_cycles=True, cycles=cycles, params=params)

    rows = [
        ("как было", naive_calls, naive_minutes),
        ("+ дедупликация в цикле", per_cycle.calls * cycles,
         per_cycle.scanned_minutes * cycles),
        ("+ сроки годности окон", full.calls, full.scanned_minutes),
    ]

    print(f"  {'вариант':<24} {'запросов':>10} {'строк просканировано':>22}")
    for name, calls, minutes in rows:
        print(f"  {name:<24} {calls:>10,} {as_rows(minutes):>22,.0f}")

    speedup = naive_minutes / full.scanned_minutes
    print(f"\n  суммарно дешевле в {speedup:.0f} раз")

    assert full.calls < per_cycle.calls * cycles
    assert speedup > 20, speedup

    print("\nOK: нагрузка на Postgres упала на два порядка")


if __name__ == "__main__":
    asyncio.run(main())
