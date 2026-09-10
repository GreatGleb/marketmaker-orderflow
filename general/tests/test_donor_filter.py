"""Отсев доноров у копибота v1: кого выбрасывать, а кого не трогать.

`copybot_v1_exclude_losing_donors` — фильтр наоборот. Он выбрасывает донора,
у которого копиры ушли в минус, и **не трогает** того, за кем никто не
копировал. Раньше правило было прямым — требовалась положительная донорская
прибыль, — и это ломало отбор тихо и сильно:

донорская история есть только у тех, кого уже выбирали донором, а выбирают
первого по собственной прибыли. Значит прямое правило пускало в пул лишь
несколько десятков инкумбентов из пятнадцати тысяч кандидатов, а на свежем
парке (`TRUNCATE test_bots`, `referral_bot_id` везде NULL) не пускало вообще
никого — половина копиботов v1 не могла начать торговать.

Отдельно проверяется окно. Донорская история смотрится за фиксированные
сутки, а не за окно самого копибота: у бота с окном в десять минут за эти
десять минут истории почти ни у кого нет, и фильтр вырождался в монетку.

Заглушки, база не нужна:

    python -m tests.test_donor_filter
"""
import asyncio

from decimal import Decimal

from app.constants.copybot import DONOR_HISTORY_MINUTES
from app.workers.profitable_bot_updater import (
    ProfitableBotUpdaterCommand as P,
    WindowCache,
)

WINDOW_MINUTES = 30

# Собственная прибыль за окно копибота: порядок отбора задаёт она.
OWN_PROFIT = [
    (101, Decimal("9")),    # BEST   — лидер, но копиры за ним потеряли
    (104, Decimal("6")),    # NOHIST — за ним никто не копировал
    (102, Decimal("3")),    # SECOND — копиры заработали
    (103, Decimal("-3")),   # LOSER  — убыточен сам, отсеивается первым шагом
]

# Чем кончилось копирование. NOHIST здесь отсутствует — это и есть
# проверяемый случай, а не пробел в фикстуре.
DONOR_PROFIT = [
    (101, Decimal("-15")),
    (102, Decimal("6")),
]

BEST, SECOND, LOSER, NOHIST = 101, 102, 103, 104


class FakeCrud:
    """Отдаёт две разные выборки и запоминает, о чём спрашивали."""

    def __init__(self):
        self.asked = []

    async def get_sorted_by_profit(
        self, since, just_not_copy_bots=False, by_referral_bot_id=False, **kw
    ):
        minutes = since.total_seconds() / 60
        self.asked.append((minutes, by_referral_bot_id))

        rows = DONOR_PROFIT if by_referral_bot_id else OWN_PROFIT

        # Форма строки как у настоящего запроса: (bot_id, P/L, сделок,
        # прибыльных). Фильтры смотрят только на первые две.
        return [(bot_id, profit, 10, 5) for bot_id, profit in rows]


async def check_no_history_is_not_a_verdict():
    """Бот без истории копирования проходит, с убыточной — нет."""
    crud = FakeCrud()

    ids = await P.filter_profitable_bots_id(
        bot_crud=crud, timeframe=WINDOW_MINUTES, exclude_losing_donors=True
    )

    assert BEST not in ids, "донор с убыточными копирами обязан вылететь"
    assert NOHIST in ids, (
        "бот без истории копирования вылетать не должен — предъявить ему "
        "нечего"
    )
    assert SECOND in ids, "донор с прибыльными копирами обязан остаться"
    assert LOSER not in ids, "убыточный сам по себе отсеивается первым шагом"

    # И порядок: побеждает самый прибыльный из уцелевших, а не тот, у кого
    # лучше донорская история. Иначе отбор превратился бы в защёлку на
    # инкумбентах — донорская прибыль есть только у тех, кого уже выбирали.
    assert ids == [NOHIST, SECOND], ids

    print(f"    с отсевом: {ids} — BEST выбыл, NOHIST без истории остался")
    print(f"      (по старому правилу «требовать плюс» осталось бы [{SECOND}])")


async def check_filter_off_keeps_everyone():
    """Без флага донорская история не спрашивается вовсе."""
    crud = FakeCrud()

    ids = await P.filter_profitable_bots_id(
        bot_crud=crud, timeframe=WINDOW_MINUTES, exclude_losing_donors=False
    )

    assert ids == [BEST, NOHIST, SECOND], ids
    assert all(not by_ref for _, by_ref in crud.asked), (
        f"лишний запрос донорской истории: {crud.asked}"
    )

    print(f"    без отсева: {ids} — донорская история не запрашивалась")


async def check_donor_window_is_fixed():
    """Донорская история смотрится за сутки, а не за окно копибота."""
    crud = FakeCrud()

    await P.filter_profitable_bots_id(
        bot_crud=crud, timeframe=WINDOW_MINUTES, exclude_losing_donors=True
    )

    donor_windows = [minutes for minutes, by_ref in crud.asked if by_ref]

    assert donor_windows == [DONOR_HISTORY_MINUTES], donor_windows
    assert DONOR_HISTORY_MINUTES != WINDOW_MINUTES, (
        "проверка бессмысленна, если окна совпали"
    )

    print(f"    окно донорской истории: {donor_windows[0]:.0f} мин "
          f"(окно копибота — {WINDOW_MINUTES})")


async def check_one_query_for_all_copybots():
    """Окно фиксированное, значит на весь парк хватает одного запроса.

    Раньше окно бралось у копибота, и донорских запросов было столько же,
    сколько различных окон, — по одному на каждое из двадцати.
    """
    crud = FakeCrud()
    cache = WindowCache()

    for timeframe in (10, 30, 720, 2880):
        await P.filter_profitable_bots_id(
            bot_crud=crud, timeframe=timeframe,
            exclude_losing_donors=True, window_cache=cache,
        )

    donor_calls = [minutes for minutes, by_ref in crud.asked if by_ref]

    assert len(donor_calls) == 1, (
        f"на четыре окна ушло {len(donor_calls)} донорских запросов вместо "
        f"одного: {donor_calls}"
    )

    print(f"    четыре разных окна копиботов → донорских запросов "
          f"{len(donor_calls)}")


async def main():
    print("Отсев провалившихся доноров:")

    await check_no_history_is_not_a_verdict()
    await check_filter_off_keeps_everyone()
    await check_donor_window_is_fixed()
    await check_one_query_for_all_copybots()

    print("  ✅ все проверки прошли")


if __name__ == "__main__":
    asyncio.run(main())
