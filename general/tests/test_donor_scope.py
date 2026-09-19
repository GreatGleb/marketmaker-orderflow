"""Пул стратегий-доноров копибота и стратегия у строк парка.

Заглушки, база не нужна.

Проверяется то, из-за чего пул вообще заведён: копибот не должен получать
донора чужой стратегии молча. Отдельно — что «ограничений нет» и «нет ни
одного кандидата» остаются разными ответами: перепутав их, легко выдать
пустому пулу весь парк.
"""
import asyncio

from decimal import Decimal

from app.constants.strategy import (
    DONOR_SCOPE_ALL,
    STRATEGY_LEGACY,
    donor_scope_keys,
    donor_scope_list,
    scope_allows,
)
from app.crud.test_bot import is_copybot_row, with_strategy
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand as P

LEGACY_ID = 1
OTHER_ID = 2

STRATEGY_ID_BY_KEY = {STRATEGY_LEGACY: LEGACY_ID, "strategy_0": OTHER_ID}

# Кандидаты приходят отсортированными по прибыльности, поэтому проверяем и
# состав, и порядок: фильтр не имеет права переставлять их местами.
DONORS = [101, 102, 103, 104]
STRATEGY_ID_BY_BOT = {
    101: LEGACY_ID,
    102: OTHER_ID,
    103: LEGACY_ID,
    104: OTHER_ID,
}


def within(scope):
    return P.donors_within_scope(
        DONORS, scope, STRATEGY_ID_BY_BOT, STRATEGY_ID_BY_KEY, copybot_id=7
    )


def check_scope_keys():
    assert donor_scope_keys(None) is None, "пустое поле — без ограничений"
    assert donor_scope_keys(DONOR_SCOPE_ALL) is None, "all — без ограничений"
    assert donor_scope_keys(donor_scope_list()) == [], "пустой список — не all"
    assert donor_scope_keys(
        donor_scope_list(STRATEGY_LEGACY, "strategy_0")
    ) == [STRATEGY_LEGACY, "strategy_0"]

    print("  пустой пул и «любая стратегия» различимы")


def check_filter():
    assert within(None) == DONORS, "без пула отбор прежний"
    assert within(DONOR_SCOPE_ALL) == DONORS, "all пропускает все стратегии"

    assert within(donor_scope_list(STRATEGY_LEGACY)) == [101, 103]
    assert within(donor_scope_list("strategy_0")) == [102, 104]
    assert within(
        donor_scope_list(STRATEGY_LEGACY, "strategy_0")
    ) == DONORS, "порядок кандидатов сохраняется"

    assert within(donor_scope_list()) == [], "пустой список — нет кандидатов"

    # Опечатка в конфиге не должна открывать пул целиком: лучше копибот
    # без донора, чем копибот с чужим алгоритмом.
    assert within(donor_scope_list("нет-такой")) == []
    assert within(donor_scope_list("нет-такой", STRATEGY_LEGACY)) == [101, 103]

    print("  фильтр по стратегии: список, all, пустой и неизвестный ключ")


def check_rows():
    rows = [
        {"symbol": "", "balance": Decimal("1000")},
        {"symbol": "", "copy_bot_min_time_profitability_min": 30},
        {"symbol": "", "copybot_v2_time_in_minutes": 60},
        {"symbol": "", "copybot_v3_time_in_minutes": 720},
    ]

    prepared = with_strategy(rows, LEGACY_ID)

    assert all(row["strategy_id"] == LEGACY_ID for row in prepared)
    assert prepared[0].get("donor_scope") is None, "обычному боту пул не нужен"

    for row in prepared[1:]:
        assert is_copybot_row(row), "копибот определяется колонкой-маркером"
        assert row["donor_scope"] == donor_scope_list(STRATEGY_LEGACY), (
            "по умолчанию пул явный: иначе копибот сменит алгоритм сам "
            "собой в день, когда появится вторая стратегия"
        )

    # Исходные строки не тронуты: сиды строят их из общих констант и
    # переиспользуют между группами.
    assert "strategy_id" not in rows[0]

    explicit = with_strategy(rows, OTHER_ID, donor_scope=DONOR_SCOPE_ALL)
    assert explicit[1]["donor_scope"] == DONOR_SCOPE_ALL

    print("  строки парка: стратегия всем, пул — только копиботам")


def check_allows():
    """Проверка по факту исполнения, а не по составу цепочки."""
    assert scope_allows(None, STRATEGY_LEGACY)
    assert scope_allows(DONOR_SCOPE_ALL, "strategy_0")
    assert scope_allows(donor_scope_list(STRATEGY_LEGACY), STRATEGY_LEGACY)
    assert not scope_allows(donor_scope_list(STRATEGY_LEGACY), "strategy_0")
    assert not scope_allows(donor_scope_list(), STRATEGY_LEGACY)
    # Стратегии нет в карте симулятора — значит и разрешать нечего.
    assert not scope_allows(donor_scope_list(STRATEGY_LEGACY), None)

    print("  исполняемая стратегия сверяется с пулом")


async def main():
    print("Пул стратегий-доноров:")
    check_allows()
    check_scope_keys()
    check_filter()
    check_rows()
    print("Готово.")


if __name__ == "__main__":
    asyncio.run(main())
