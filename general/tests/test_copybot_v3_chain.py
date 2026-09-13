"""Цепочка отбора у копибота v3 и её главная опасность — кольцо.

v3 берёт лучшего копибота v2, тот лучшего v1, а тот уже обычного бота. Той же
цепочкой ходит боевой `binance_bot`, поэтому сделки v3 — прогноз реальной
торговли, и ошибка в любом звене делает прогноз бессмысленным.

Отдельно проверяется то, чем эта конструкция ломается молча. Тип бота нигде не
хранится отдельным полем: он выводится из того, какая колонка-маркер не NULL, а
«обычный бот» — это перечисление «все маркеры NULL». Забудь в этом перечислении
колонку v3 — и копибот v3 попадёт в пул доноров для v1. Цепочка замкнётся в
кольцо v3 → v2 → v1 → v3, а собственных торговых параметров у копибота нет:
конфиг соберётся из нулей. Ничего не упадёт, в логах не появится ни строчки.

Заглушки, база не нужна:

    python -m tests.test_copybot_v3_chain
"""
import asyncio

from decimal import Decimal

from app.crud.test_bot import COPYBOT_MARKER_COLUMNS
from app.db.models import TestBot
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand as P

V3_WINDOW = 720
V2_WINDOW = 60

# id по уровням лестницы. Разнесены по сотням, чтобы в сообщениях об ошибках
# сразу было видно, на каком уровне отбор пошёл не туда.
PLAIN_BOT, COPYBOT_V1, COPYBOT_V2, COPYBOT_V3 = 101, 201, 301, 401


def make_bot(bot_id, **fields) -> TestBot:
    fields.setdefault('symbol', '')

    return TestBot(id=bot_id, balance=Decimal("1000"), **fields)


BOTS = {
    PLAIN_BOT: make_bot(PLAIN_BOT, symbol='BTCUSDT', start_updown_ticks=5),
    COPYBOT_V1: make_bot(
        COPYBOT_V1, copy_bot_min_time_profitability_min=Decimal("30")
    ),
    COPYBOT_V2: make_bot(
        COPYBOT_V2, copybot_v2_time_in_minutes=Decimal(str(V2_WINDOW))
    ),
    COPYBOT_V3: make_bot(
        COPYBOT_V3, copybot_v3_time_in_minutes=Decimal(str(V3_WINDOW))
    ),
}


class FakeCrud:
    """Возвращает ботов того уровня, о котором спросили.

    Запоминает запросы: проверяется не только результат, но и то, за какое
    окно и по какому уровню спрашивали. Окно на втором шаге должно приходить
    от найденного v2, а не от самого v3 — перепутать их легко, а заметить по
    одному лишь итоговому конфигу нельзя.
    """

    def __init__(self, pools=None):
        self.asked = []
        self.pools = pools if pools is not None else {
            1: [(COPYBOT_V1, Decimal("5"))],
            2: [(COPYBOT_V2, Decimal("7"))],
        }

    async def get_sorted_by_profit(
        self, since, just_copy_bots=False, just_copy_bots_v2=False,
        just_copy_bots_v3=False, just_not_copy_bots=False, **kw
    ):
        minutes = since.total_seconds() / 60

        if just_copy_bots:
            level = 1
        elif just_copy_bots_v2:
            level = 2
        elif just_copy_bots_v3:
            level = 3
        else:
            level = 0

        self.asked.append((minutes, level))

        return [
            (bot_id, profit, 10, 5)
            for bot_id, profit in self.pools.get(level, [])
        ]

    async def get_bot_by_id(self, bot_id):
        bot = BOTS.get(bot_id)

        return [bot] if bot else []


async def check_v3_descends_to_copybot_v1():
    """v3 спрашивает копиботов v2 за своё окно, а v1 — за окно найденного v2."""
    crud = FakeCrud()

    copybot_v2 = await P.get_copybot_config(
        bot_crud=crud,
        copybot_v2_time_in_minutes=V3_WINDOW,
        donor_version=2,
    )

    assert copybot_v2 is not None, (
        "v3 не нашёл копибота v2 — дальше спускаться не с чем"
    )
    assert copybot_v2.id == COPYBOT_V2, (
        f"на первом шаге выбран {copybot_v2.id}, а не копибот v2"
    )

    copybot_v1 = await P.get_copybot_config(
        bot_crud=crud,
        copybot_v2_time_in_minutes=copybot_v2.copybot_v2_time_in_minutes,
        donor_version=1,
    )

    assert copybot_v1.id == COPYBOT_V1, (
        f"на втором шаге выбран {copybot_v1.id}, а не копибот v1"
    )

    assert crud.asked == [(V3_WINDOW, 2), (V2_WINDOW, 1)], (
        f"спрашивали {crud.asked}: окно второго шага должно приходить от "
        f"найденного копибота v2 ({V2_WINDOW} мин), а не от самого v3"
    )

    print(f"  v3 → v2 ({COPYBOT_V2}) → v1 ({COPYBOT_V1}), окна не перепутаны")


async def check_broken_chain_returns_nothing():
    """Обрыв на любом уровне даёт None, а не полупустой конфиг."""
    for level, name in ((2, 'копиботов v2'), (1, 'копиботов v1')):
        crud = FakeCrud(pools={level: []})

        found = await P.get_copybot_config(
            bot_crud=crud,
            copybot_v2_time_in_minutes=V3_WINDOW,
            donor_version=level,
        )

        assert found is None, (
            f"при пустом пуле {name} вернулся {found} — бот стал бы торговать "
            f"конфигом, которого никто не выбирал"
        )

    print("  пустой пул на любом уровне — None, а не мусор")


async def check_losing_donors_are_not_picked():
    """Убыточный кандидат не становится донором даже в одиночестве."""
    crud = FakeCrud(pools={2: [(COPYBOT_V2, Decimal("-3"))]})

    found = await P.get_copybot_config(
        bot_crud=crud,
        copybot_v2_time_in_minutes=V3_WINDOW,
        donor_version=2,
    )

    assert found is None, (
        "выбран убыточный копибот v2: копировать того, кто теряет деньги, "
        "смысла нет ни на каком уровне"
    )

    print("  убыточный кандидат отсеян")


def check_v3_counts_as_copybot():
    """Колонка v3 попала в перечисление маркеров — иначе кольцо."""
    assert "copybot_v3_time_in_minutes" in COPYBOT_MARKER_COLUMNS, (
        "колонки v3 нет в COPYBOT_MARKER_COLUMNS: копибот v3 попадёт в пул "
        "доноров для v1, цепочка замкнётся в кольцо, и донор соберётся из "
        "нулей — молча, без единой ошибки в логах"
    )

    print(f"  маркеры копиботов: {', '.join(COPYBOT_MARKER_COLUMNS)}")


async def main():
    print("Проверяем цепочку отбора копибота v3")

    check_v3_counts_as_copybot()
    await check_v3_descends_to_copybot_v1()
    await check_broken_chain_returns_nothing()
    await check_losing_donors_are_not_picked()

    print("✅ все проверки прошли")


if __name__ == "__main__":
    asyncio.run(main())
