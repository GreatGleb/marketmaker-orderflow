"""Сколько агрегатов по test_orders делает воркер копиботов за один цикл.

Конфигурация как в new_bots.py: 20 окон × флаг «прибылен за 24 ч» × флаг
«отсеять провалившихся доноров» = 80 копиботов.

Оба дополнительных фильтра смотрят на фиксированное окно в сутки, поэтому
на все 80 копиботов приходится по одному запросу на каждый — а не по одному
на каждое из двадцати окон. Отсюда 21 вместо прежних 40.
"""
import asyncio
from decimal import Decimal

from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand as P

WINDOWS = [10, 20, 30, 40, 50, 60, 120, 180, 240, 360, 420, 480, 540,
           600, 660, 720, 1440, 1920, 2400, 2880]


class CountingCrud:
    """Считает обращения к get_sorted_by_profit и запомненные окна."""

    def __init__(self):
        self.calls = []

    async def get_sorted_by_profit(self, since, just_not_copy_bots=False,
                                   by_referral_bot_id=False, **kw):
        minutes = since.total_seconds() / 60
        self.calls.append((minutes, by_referral_bot_id))

        # Немного правдоподобных данных: три прибыльных бота.
        return [
            (101, Decimal("500"), 10, 8),
            (102, Decimal("300"), 8, 5),
            (103, Decimal("-50"), 4, 1),
        ]


def build_params():
    params = {}
    bot_id = 1

    for window in WINDOWS:
        for check_24h in (False, True):
            for no_losing in (False, True):
                params[bot_id] = {"tf": window, "24h": check_24h,
                                  "no_losing_donors": no_losing}
                bot_id += 1

    return params


async def main():
    params = build_params()
    print(f"  копиботов: {len(params)}, различных окон: {len(WINDOWS)}")

    crud = CountingCrud()
    result = await P.get_profitable_bots_id_by_individual_params(
        bot_crud=crud, bot_profitability_parameters=params
    )

    distinct = set(crud.calls)
    print(f"  запросов к БД: {len(crud.calls)}")
    print(f"  различных среди них: {len(distinct)}")

    # Без дедупликации было бы: 80 базовых + 40 суточных + 40 донорских.
    assert len(crud.calls) == len(distinct), (
        f"есть повторы: {len(crud.calls)} запросов на {len(distinct)} разных"
    )
    # 20 запросов на окна ботов + 1 донорский за сутки. Суточная проверка
    # прибыльности своего запроса не добавляет: её окно 1440 минут уже есть
    # в сетке, и кэш отдаёт готовое.
    assert len(crud.calls) == 21, len(crud.calls)

    # Результат не должен пострадать: убыточный бот отсеян, порядок сохранён.
    assert result[1] == [101, 102], result[1]
    assert len(result) == len(params)

    print(f"  было бы без кэша: 160 → стало {len(crud.calls)}, "
          f"в {160 / len(crud.calls):.1f} раза меньше")
    print("\nOK: каждое окно считается один раз за цикл")


if __name__ == "__main__":
    asyncio.run(main())
