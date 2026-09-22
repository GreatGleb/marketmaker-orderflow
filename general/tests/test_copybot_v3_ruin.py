"""Разорение компаундирующего копибота v3: счёт восстанавливается, а не конец.

Раньше десять нехваток подряд означали смерть: бот получал
`copybot_v3_stopped_at` и больше не торговал никогда. Прогноз на этом
обрывался, и по нему нельзя было отличить «слил счёт один раз за месяц» от
«сливает каждые двое суток» — в обоих случаях в отчёте стояла одна дата.

Теперь счёт возвращается к стартовому, а разорения считаются. Число разорений
и есть результат прогноза: столько раз боевой бот завёл бы новый депозит.

Насовсем бот останавливается в одном случае — если после восстановления не
прошло ни одной сделки. Значит стартового счёта не хватает на лот в принципе,
и возвращать его снова незачем.

Заглушки, база не нужна:

    python -m tests.test_copybot_v3_ruin
"""
import asyncio

from decimal import Decimal

from app.bots.demo_test_bot import Refusal, StartTestBotsCommand as S
from app.constants.copybot import COPYBOT_V3_START_BALANCE

BOT_ID = 777
SHORTAGE = Refusal("shortage", "на счёте 3.10 не набирается минимальный лот")


class FakeCommand:
    """Только то состояние v3, которого касается разорение."""

    def __init__(self):
        self._v3_balances = {}
        self._v3_stopped = set()
        self._v3_shortages = {}
        self._v3_traded_since_ruin = set()
        self.ruins = []
        self.stops = []

    # Вместо похода в базу — запись факта.
    async def _mark_ruin(self, bot_id, balance):
        self.ruins.append((bot_id, balance))

    _v3_note_shortage = S._v3_note_shortage
    COMPOUND_SHORTAGE_LIMIT = S.COMPOUND_SHORTAGE_LIMIT

    async def _v3_ruin(self, bot_id, reason):
        """Копия боевой ветвлёнки без обращения к базе."""
        if bot_id in self._v3_stopped:
            return

        if bot_id not in self._v3_traded_since_ruin:
            await self._v3_stop(bot_id=bot_id, reason=reason)
            return

        self._v3_balances[bot_id] = COPYBOT_V3_START_BALANCE
        self._v3_shortages.pop(bot_id, None)
        self._v3_traded_since_ruin.discard(bot_id)
        await self._mark_ruin(bot_id, COPYBOT_V3_START_BALANCE)

    async def _v3_stop(self, bot_id, reason):
        self._v3_stopped.add(bot_id)
        self.stops.append((bot_id, reason))


async def shortages(command, times):
    for _ in range(times):
        await command._v3_note_shortage(BOT_ID, SHORTAGE)


async def check_ruin_restores_balance_and_counts():
    """Десять нехваток подряд: счёт стартовый, бот жив, разорение посчитано."""
    command = FakeCommand()
    command._v3_balances[BOT_ID] = Decimal("3.10")
    # Бот торговал — значит дистанцию прошёл, восстановление осмысленно.
    command._v3_traded_since_ruin.add(BOT_ID)

    await shortages(command, S.COMPOUND_SHORTAGE_LIMIT)

    assert command.ruins == [(BOT_ID, COPYBOT_V3_START_BALANCE)], (
        f"разорение не записано: {command.ruins}. В отчёте бот выглядел бы "
        f"торгующим без единой отметки о сливе счёта"
    )
    assert command._v3_balances[BOT_ID] == COPYBOT_V3_START_BALANCE, (
        f"счёт остался {command._v3_balances[BOT_ID]}, а не "
        f"{COPYBOT_V3_START_BALANCE}: бот продолжил бы упираться в нехватку"
    )
    assert not command._v3_stopped, (
        "бот остановлен насовсем, хотя до этого торговал — прогноз снова "
        "обрывается на первом же сливе"
    )
    assert BOT_ID not in command._v3_shortages, (
        "счётчик нехваток не обнулён: следующая же неудача засчиталась бы "
        "как десятая и разорила бы бота повторно"
    )

    print(f"  разорение: счёт {COPYBOT_V3_START_BALANCE}, бот торгует дальше")


async def check_shortages_below_limit_do_not_ruin():
    """Девяти неудач мало: донор меняется, следующая пара может подойти."""
    command = FakeCommand()
    command._v3_balances[BOT_ID] = Decimal("3.10")
    command._v3_traded_since_ruin.add(BOT_ID)

    await shortages(command, S.COMPOUND_SHORTAGE_LIMIT - 1)

    assert not command.ruins, (
        "бот разорён раньше предела: одна экзотическая пара обнуляла бы счёт, "
        "и разорения перестали бы что-либо значить"
    )
    assert command._v3_shortages[BOT_ID] == S.COMPOUND_SHORTAGE_LIMIT - 1

    print(f"  {S.COMPOUND_SHORTAGE_LIMIT - 1} неудач подряд: счёт не тронут")


async def check_pair_refusals_never_ruin():
    """Отказ по паре не про счёт — он не приближает разорение."""
    command = FakeCommand()
    command._v3_traded_since_ruin.add(BOT_ID)
    pair_refusal = Refusal("pair", "количество выше maxQty")

    for _ in range(S.COMPOUND_SHORTAGE_LIMIT * 2):
        await command._v3_note_shortage(BOT_ID, pair_refusal)

    assert not command.ruins and not command._v3_stopped, (
        "отказы по паре разорили бота: на экзотических парах донора счёт "
        "обнулялся бы на ровном месте"
    )

    print("  отказы по паре: счётчик разорений не двигается")


async def check_ruin_without_trades_stops_for_good():
    """Восстановили и ни одной сделки — дальше восстанавливать бессмысленно."""
    command = FakeCommand()
    command._v3_balances[BOT_ID] = Decimal("3.10")
    # Ключевое отличие: после прошлого восстановления бот не торговал.

    await shortages(command, S.COMPOUND_SHORTAGE_LIMIT)

    assert not command.ruins, (
        "счёт восстановлен боту, который на нём не смог совершить ни одной "
        "сделки — получился бы вечный цикл «восстановил — разорился»"
    )
    assert command._v3_stopped == {BOT_ID}, (
        "бот не остановлен насовсем, хотя стартового счёта ему не хватает"
    )

    print("  разорение без сделок: остановка насовсем, цикла нет")


async def main():
    print("Проверяем разорение компаундирующего копибота v3")

    await check_ruin_restores_balance_and_counts()
    await check_shortages_below_limit_do_not_ruin()
    await check_pair_refusals_never_ruin()
    await check_ruin_without_trades_stops_for_good()

    print("✅ всё сошлось")


if __name__ == "__main__":
    asyncio.run(main())
