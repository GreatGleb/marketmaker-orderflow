"""Воркер set_profitable_bot берёт сессию на один цикл, а не на весь процесс.

Зависимости `Command` решаются один раз в `run_async`, а цикл живёт внутри
`command()` — сессия из зависимостей оказалась бы одна на весь процесс.
Коммита у воркера нет, он только читает, поэтому такая сессия держала бы одну
транзакцию до остановки процесса, а её снапшот не даёт autovacuum вычистить
мёртвые строки, появившиеся после начала транзакции. По `test_orders` это
десятки миллионов строк в сутки: ретеншн их удаляет, а место не возвращается.

Проверяется на заглушках: сколько циклов — столько сессий, и к моменту сна
между циклами ни одной открытой не остаётся.
"""
import asyncio
import contextlib
from decimal import Decimal
from unittest.mock import patch

import app.workers.profitable_bot_updater as m
from app.db.models import TestBot
from app.workers.profitable_bot_updater import ProfitableBotUpdaterCommand

CYCLES = 3


class FakeSession:
    def __init__(self):
        self.closed = False
        self.commits = 0

    async def commit(self):
        self.commits += 1


class FakeSessionManager:
    """Подмена DatabaseSessionManager: та же форма, без базы."""

    def __init__(self):
        self.sessions = []

    @classmethod
    def create(cls, url):  # noqa: ARG003 — url не нужен
        return cls.instance

    @contextlib.asynccontextmanager
    async def get_session(self):
        session = FakeSession()
        self.sessions.append(session)
        try:
            yield session
        finally:
            # Настоящий get_session закрывает сессию в finally — вместе с ней
            # заканчивается и транзакция.
            session.closed = True


class FakeCrud:
    """Минимум методов, которые дёргает command(): копиботы, агрегат, донор."""

    def __init__(self, session):
        self.session = session
        self.copybot = TestBot(
            id=7,
            symbol="",
            balance=Decimal("1000"),
            copy_bot_min_time_profitability_min=Decimal("30"),
            copybot_v1_check_for_24h_profitability=False,
            copybot_v1_exclude_losing_donors=False,
        )
        self.donor = TestBot(
            id=101,
            symbol="BMTUSDT",
            balance=Decimal("1000"),
            stop_success_ticks=20,
            stop_loss_ticks=30,
            start_updown_ticks=10,
            use_trailing_stop=False,
            consider_ma_for_open_order=False,
            consider_ma_for_close_order=False,
        )

    async def get_copybots(self):
        return [self.copybot]

    async def get_sorted_by_profit(self, since, **kwargs):
        return [(self.donor.id, Decimal("5"), 3, 2)]

    async def get_bot_by_id(self, bot_id):
        return [self.donor]


class FakeRedis:
    def __init__(self):
        self.keys = {}

    async def set(self, key, value):
        self.keys[key] = value


async def main():
    stop_event = asyncio.Event()
    command = ProfitableBotUpdaterCommand(stop_event=stop_event)
    manager = FakeSessionManager()
    FakeSessionManager.instance = manager
    redis = FakeRedis()

    slept = []

    async def fake_sleep(seconds):
        # Сон — единственная точка, где известно, что цикл дошёл до конца.
        slept.append(seconds)

        open_now = [s for s in manager.sessions if not s.closed]
        assert not open_now, (
            f"на момент сна открыто сессий: {len(open_now)} — транзакция "
            f"переживает цикл, и её снапшот держит мёртвые строки от VACUUM"
        )

        if len(slept) >= CYCLES:
            stop_event.set()

    with patch.object(m, "DatabaseSessionManager", FakeSessionManager), \
            patch.object(m, "TestBotCrud", FakeCrud), \
            patch.object(m.asyncio, "sleep", fake_sleep):
        await command.command(redis=redis)

    assert len(slept) == CYCLES, f"циклов прошло {len(slept)}, ждали {CYCLES}"
    print(f"  циклов воркера: {len(slept)}, сон между ними: {slept[0]} с")

    assert len(manager.sessions) == CYCLES, (
        f"сессий создано {len(manager.sessions)} на {CYCLES} циклов — "
        f"значит одна переиспользуется между циклами"
    )
    print(f"  сессий создано: {len(manager.sessions)} (по одной на цикл)")

    assert all(s.closed for s in manager.sessions), "сессия осталась открытой"
    print("  все сессии закрыты")

    assert all(s.commits == 0 for s in manager.sessions), (
        "воркер только читает, commit здесь означал бы запись в test_bots"
    )
    print("  commit не вызывался ни разу")

    # Смысл цикла при этом не потерялся: ключ донора в Redis обновлён.
    assert redis.keys.get("copy_bot_7"), "ключ copy_bot_7 не записан"
    print(f"  ключи в Redis: {sorted(redis.keys)}")

    print("\nвсё сошлось")


if __name__ == "__main__":
    asyncio.run(main())
