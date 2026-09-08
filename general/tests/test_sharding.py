"""Проверка шардирования симулятора.

Парк делится между процессами по остатку от деления id. Две вещи, которые
здесь важно не сломать: отбор ботов в шарде (бот должен достаться ровно
одному процессу — иначе он либо торгует дважды, либо не торгует вообще) и
управление группой процессов из supervisor_control (по имени `test_bots`
supervisorctl больше ничего не находит, см. gotcha про `no such process`).
"""
import asyncio
from unittest.mock import patch

from sqlalchemy.dialects import postgresql

from app.bots.demo_test_bot import StartTestBotsCommand
from app.crud.test_bot import TestBotCrud
from app.scripts import supervisor_control as sc

FULL_PARK = 17_236


class FakeResult:
    def scalars(self):
        return self

    def all(self):
        return []


class FakeSession:
    """Ловит запрос, до базы не ходит."""

    def __init__(self):
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return FakeResult()


async def where_clause(shard, shards):
    session = FakeSession()
    await TestBotCrud(session).get_active_bots(shard=shard, shards=shards)

    sql = str(
        session.stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    # psycopg2 экранирует знак остатка в `%%` — читаемости это не добавляет.
    return sql.replace("%%", "%").split("WHERE", 1)[1].strip()


class FakeSupervisorctl:
    """Заглушка вместо `supervisorctl`: помнит состояния и записывает команды."""

    def __init__(self, states, broken=False):
        self.states = dict(states)
        self.broken = broken
        self.commands = []

    def __call__(self, *args):
        if self.broken:
            raise FileNotFoundError("supervisorctl")

        self.commands.append(args)
        command = args[0]

        if command == "status":
            lines = [
                f"{name:<32} {state}   pid 1, uptime 0:00:01"
                for name, state in self.states.items()
            ]
            return self._result("\n".join(lines))

        if args[1] not in self.states:
            # supervisorctl отвечает кодом 1 на неизвестное имя процесса.
            return self._result(f"{args[1]}: ERROR (no such process)", code=1)

        if command == "stop":
            self.states[args[1]] = "STOPPED"
        elif command in ("start", "restart"):
            self.states[args[1]] = "RUNNING"

        return self._result("")

    @staticmethod
    def _result(stdout, code=0):
        return type(
            "R", (), {"stdout": stdout, "stderr": "", "returncode": code}
        )


SHARDED = {
    "fastapi": "RUNNING",
    "candles_history": "STOPPED",
    "test_bots:test_bots_00": "RUNNING",
    "test_bots:test_bots_01": "RUNNING",
    "test_bots:test_bots_02": "STOPPED",
    "test_bots:test_bots_03": "RUNNING",
}


async def main():
    print("=== 1. один процесс = весь парк, без лишнего условия ===")
    clause = await where_clause(shard=0, shards=1)
    print(f"  WHERE {clause}")
    assert "%" not in clause, "при shards=1 остаток в запросе не нужен"

    print("\n=== 2. четыре шарда = четыре непересекающихся доли ===")
    for shard in range(4):
        clause = await where_clause(shard=shard, shards=4)
        print(f"  шард {shard}: WHERE {clause}")
        assert "test_bots.id % 4" in clause
        assert clause.endswith(f"= {shard}")

    print("\n=== 3. каждый бот достаётся ровно одному шарду ===")
    for shards in (1, 2, 4, 7):
        owners = [
            sum(1 for shard in range(shards) if bot_id % shards == shard)
            for bot_id in range(1, FULL_PARK + 1)
        ]
        sizes = [
            sum(1 for bot_id in range(1, FULL_PARK + 1) if bot_id % shards == shard)
            for shard in range(shards)
        ]
        print(f"  шардов {shards}: по ботам {min(sizes)}–{max(sizes)}")
        assert set(owners) == {1}, "бот без шарда или сразу в двух"
        assert sum(sizes) == FULL_PARK
        # Перекос больше чем на одного бота означал бы, что один процесс
        # упрётся в потолок раньше остальных.
        assert max(sizes) - min(sizes) <= 1

    print("\n=== 4. рекомендация 4–7 тысяч ботов на процесс ===")
    per_process = FULL_PARK / 4
    limit = StartTestBotsCommand.MAX_BOTS_PER_SHARD
    print(f"  полный парк на четырёх процессах: {per_process:.0f} на процесс, "
          f"порог предупреждения {limit}")
    assert per_process <= limit, "четырёх процессов на полный парк не хватает"
    # Нижняя граница правила: меньше четырёх тысяч на процесс — это лишние
    # процессы, каждый из которых заново строит свой shared_data.
    assert per_process >= 4_000

    print("\n=== 5. имя программы разворачивается в процессы группы ===")
    fake = FakeSupervisorctl(SHARDED)

    with patch.object(sc, "_run", fake):
        names = sc.process_names("test_bots")
        print(f"  test_bots -> {names}")
        assert names == [
            "test_bots:test_bots_00",
            "test_bots:test_bots_01",
            "test_bots:test_bots_02",
            "test_bots:test_bots_03",
        ]
        assert sc.process_names("fastapi") == ["fastapi"]
        assert sc.is_running("test_bots") is True
        assert sc.is_running("candles_history") is False

    print("\n=== 6. paused поднимает обратно только то, что работало ===")
    fake = FakeSupervisorctl(SHARDED)

    with patch.object(sc, "_run", fake):
        with sc.paused("test_bots"):
            inside = sc.running_processes("test_bots")
            print(f"  внутри блока работает: {inside}")
            assert inside == []

        after = sc.running_processes("test_bots")

    print(f"  после блока работает: {after}")
    # Шард 02 был выключен вручную — сам он подниматься не должен.
    assert after == [
        "test_bots:test_bots_00",
        "test_bots:test_bots_01",
        "test_bots:test_bots_03",
    ]
    assert ("start", "test_bots:test_bots_02") not in fake.commands
    # Но команду stop он получает вместе со всеми: для остановленного это
    # ничего не делает, зато не остаётся состояния, из которого процесс
    # поднимется в середине блока.
    assert ("stop", "test_bots:test_bots_02") in fake.commands
    # Соседние программы paused("test_bots") трогать не должен.
    assert not any(
        len(c) > 1 and not c[1].startswith("test_bots") for c in fake.commands
    )

    print("\n=== 7. падающий шард не проскочит внутрь блока ===")
    # BACKOFF — процесс упал и ждёт очередной попытки запуска, STARTING —
    # только что запущен. И тот и другой сами придут в RUNNING, поэтому для
    # paused они «работают»: остановить и вернуть как было.
    crashing = FakeSupervisorctl({
        "test_bots:test_bots_00": "RUNNING",
        "test_bots:test_bots_01": "BACKOFF",
        "test_bots:test_bots_02": "STARTING",
        "test_bots:test_bots_03": "FATAL",
    })

    with patch.object(sc, "_run", crashing):
        with sc.paused("test_bots") as was_running:
            print(f"  остановлено: {was_running}")
            assert was_running == [
                "test_bots:test_bots_00",
                "test_bots:test_bots_01",
                "test_bots:test_bots_02",
            ]
            assert sc.running_processes("test_bots") == []

    # FATAL — supervisord уже отказался его перезапускать, сам он не оживёт;
    # поднимать его вместо человека мы не будем.
    assert ("start", "test_bots:test_bots_03") not in crashing.commands
    assert crashing.states["test_bots:test_bots_03"] == "STOPPED"
    print("  падавшие шарды остановлены и возвращены, FATAL оставлен человеку")

    print("\n=== 8. без supervisorctl ничего не падает ===")
    broken = FakeSupervisorctl(SHARDED, broken=True)

    with patch.object(sc, "_run", broken):
        assert sc.is_running("test_bots") is False
        assert sc.process_names("test_bots") == ["test_bots"]

        with sc.paused("test_bots") as was_running:
            assert was_running == []

    print("  вне контейнера остановка просто ничего не делает")

    print("\nOK: парк делится без пропусков, группа процессов управляется целиком")


asyncio.run(main())
