"""Симулятор, запущенный руками, не даст испортить данные.

`supervisor_control.paused()` останавливает процессы через supervisorctl, а
симулятор, запущенный руками (`python -m app.scripts.start_test_bots`),
supervisorctl не подчиняется: пересборка `watched_pair` или парка ботов прошла
бы под живым процессом. Поэтому каждый процесс симулятора держит в Redis свой
флаг, а `paused()` перед остановкой смотрит, не остался ли тот, кого он не
остановит.

Проверяется на заглушке вместо Redis: ключ на процесс (а не счётчик, который
течёт после SIGKILL), TTL и продление, снятие флага на любом выходе из блока,
и главное — кого `paused()` считает поводом отказаться. Пункт 4.4
.ai/docs/test-bots/09-roadmap.md.
"""
import asyncio
import json
from unittest.mock import patch

from app.scripts import simulator_flag as sf
from app.scripts import supervisor_control as sc

# Тот же расклад процессов, что в tests/test_sharding.py. Скопирован, а не
# импортирован: тот файл — скрипт, он запускает свои проверки прямо на импорте.
SHARDED = {
    "fastapi": "RUNNING",
    "candles_history": "STOPPED",
    "test_bots:test_bots_00": "RUNNING",
    "test_bots:test_bots_01": "RUNNING",
    "test_bots:test_bots_02": "STOPPED",
    "test_bots:test_bots_03": "RUNNING",
}


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

        if args[0] == "status":
            return self._result("\n".join(
                f"{name:<32} {state}   pid 1, uptime 0:00:01"
                for name, state in self.states.items()
            ))

        if args[0] == "stop":
            self.states[args[1]] = "STOPPED"
        elif args[0] in ("start", "restart"):
            self.states[args[1]] = "RUNNING"

        return self._result("")

    @staticmethod
    def _result(stdout, code=0):
        return type("R", (), {"stdout": stdout, "stderr": "", "returncode": code})


class FakeRedis:
    """Заглушка вместо Redis: синхронное чтение и асинхронная запись в одном
    объекте — ровно те методы, которыми пользуется simulator_flag."""

    def __init__(self, broken=False):
        self.broken = broken
        self.data = {}
        self.ttls = {}
        self.sets = 0
        self.closed = False

    # --- то, чем пользуются скрипты (синхронный клиент) ---
    def scan_iter(self, match=None, count=None):  # noqa: ARG002
        if self.broken:
            raise ConnectionError("Redis недоступен")

        prefix = match.rstrip("*")

        return [key for key in list(self.data) if key.startswith(prefix)]

    def get(self, key):
        if self.broken:
            raise ConnectionError("Redis недоступен")

        return self.data.get(key)

    def close(self):
        self.closed = True

    # --- то, чем пользуется симулятор (асинхронный клиент) ---
    async def set(self, key, value, ex=None):
        self.data[key] = value
        self.ttls[key] = ex
        self.sets += 1

    async def delete(self, key):
        self.data.pop(key, None)
        self.ttls.pop(key, None)

    async def aclose(self):
        self.closed = True

    # --- то, чего Redis делает сам, а заглушке надо сказать ---
    def expire(self, key):
        self.data.pop(key, None)


def value_of(shard, shards, pid, host="general", supervisor=""):
    """Значение флага так, как его пишет процесс симулятора."""
    return json.dumps({
        "host": host, "pid": pid, "shard": shard, "shards": shards,
        "supervisor": supervisor, "started_at": "2026-09-09T12:00:00+00:00",
    })


async def main():
    print("=== 1. флаг поднимается на входе в блок и снимается на выходе ===")
    fake = FakeRedis()

    async with sf.hold(shard=1, shards=4, client=fake) as key:
        print(f"  ключ {key}, TTL {fake.ttls[key]} с")
        assert key in fake.data
        assert fake.ttls[key] == sf.TTL_SECONDS
        # Ключ строится из host и pid, а не из номера шарда: два процесса с
        # одинаковым --shard (ручной запуск поверх supervisord — как раз этот
        # случай) не должны затирать флаги друг друга.
        assert key == sf.own_key()
        assert str(1) not in key.rsplit(":", 2)[0]

        [live] = sf.live_simulators(client=fake)
        print(f"  скрипт видит: {live}")
        assert (live.shard, live.shards) == (1, 4)
        assert live.under_supervisor is False

    assert fake.data == {}, "флаг остался висеть после выхода из блока"
    # Соединение чужое (передали снаружи) — закрывать его не наше дело.
    assert fake.closed is False
    print("  после блока флагов нет")

    print("\n=== 2. флаг продлевается чаще, чем истекает ===")
    # Три подряд пропущенных продления флаг ещё переживает: событийный цикл
    # симулятора под нагрузкой просыпается не секунда в секунду.
    assert sf.REFRESH_INTERVAL_SECONDS * 3 < sf.TTL_SECONDS
    print(f"  TTL {sf.TTL_SECONDS} с, продление раз в "
          f"{sf.REFRESH_INTERVAL_SECONDS} с")

    fake = FakeRedis()

    with patch.object(sf, "REFRESH_INTERVAL_SECONDS", 0.01):
        async with sf.hold(shard=0, shards=1, client=fake) as key:
            await asyncio.sleep(0.05)
            renewals = fake.sets - 1
            print(f"  продлений за 5 интервалов: {renewals}")
            assert renewals >= 3
            # Продление возвращает полный TTL, а не остаток.
            assert fake.ttls[key] == sf.TTL_SECONDS

    print("\n=== 3. флаг снимается и когда блок кончился ошибкой ===")
    fake = FakeRedis()

    try:
        async with sf.hold(shard=0, shards=1, client=fake):
            assert fake.data
            raise RuntimeError("симулятор упал")
    except RuntimeError:
        pass

    assert fake.data == {}
    print("  упавший процесс флаг за собой убрал")

    print("\n=== 4. упавший по SIGKILL процесс разблокирует TTL ===")
    # Счётчик (INCR/DECR) здесь бы навсегда остался больше нуля: DECR сделать
    # уже некому. Ключ с TTL Redis удаляет сам.
    fake = FakeRedis()
    fake.data[f"{sf.KEY_PREFIX}:general:812"] = value_of(0, 1, 812)
    assert len(sf.live_simulators(client=fake)) == 1
    fake.expire(f"{sf.KEY_PREFIX}:general:812")
    assert sf.live_simulators(client=fake) == []
    print("  флаг убитого процесса истёк — блокировки не осталось")

    print("\n=== 5. paused отказывается работать под ручным симулятором ===")
    manual = [sf.Simulator(
        key=f"{sf.KEY_PREFIX}:general:812", host="general", pid=812,
        shard=0, shards=1, supervisor="", started_at="2026-09-09T12:00:00",
    )]
    fake_ctl = FakeSupervisorctl(SHARDED)

    with patch.object(sc, "_run", fake_ctl), \
            patch.object(sc, "live_simulators", lambda: manual):
        try:
            with sc.paused("test_bots"):
                assert False, "блок не должен был начаться"
        except sf.SimulatorIsRunning as e:
            message = str(e)

    print("  " + message.splitlines()[0])
    assert "pid 812" in message
    # Проверка идёт до первого stop: незачем гасить парк ради операции,
    # которая всё равно не начнётся.
    assert fake_ctl.commands == [("status",)], fake_ctl.commands
    print("  ни один шард не остановлен")

    print("\n=== 6. шарды под supervisord работать не мешают ===")
    # Их остановит сам paused. Больше того, после `supervisorctl stop` их флаги
    # висят до TTL (SIGTERM не даёт выполниться finally) — если считать их
    # поводом для отказа, скрипт заблокирует сам себя.
    supervised = [
        sf.Simulator(
            key=f"{sf.KEY_PREFIX}:general:{100 + shard}", host="general",
            pid=100 + shard, shard=shard, shards=4,
            supervisor=f"test_bots_{shard:02d}",
            started_at="2026-09-09T12:00:00",
        )
        for shard in range(4)
    ]
    fake_ctl = FakeSupervisorctl(SHARDED)

    with patch.object(sc, "_run", fake_ctl), \
            patch.object(sc, "live_simulators", lambda: supervised):
        with sc.paused("test_bots") as was_running:
            assert sc.running_processes("test_bots") == []

    print(f"  остановлено и возвращено: {len(was_running)} шарда")
    assert len(was_running) == 3

    print("\n=== 7. без ответа supervisorctl блокирует любой живой шард ===")
    # supervisorctl не ответил — значит не остановлено вообще ничего, и шард
    # под supervisord тоже повод отказаться.
    broken_ctl = FakeSupervisorctl(SHARDED, broken=True)

    with patch.object(sc, "_run", broken_ctl), \
            patch.object(sc, "live_simulators", lambda: supervised):
        try:
            with sc.paused("test_bots"):
                assert False, "блок не должен был начаться"
        except sf.SimulatorIsRunning as e:
            message = str(e)

    print("  " + message.splitlines()[0])
    assert "supervisorctl не ответил" in message

    print("\n=== 8. недоступный Redis не ломает скрипты ===")
    # Так же ведёт себя недоступный supervisorctl: пишем предупреждение и
    # работаем дальше. Симулятор без Redis всё равно не торгует — цены он
    # берёт оттуда же.
    assert sf.live_simulators(client=FakeRedis(broken=True)) == []

    fake_ctl = FakeSupervisorctl(SHARDED)

    with patch.object(sc, "_run", fake_ctl), \
            patch.object(sc, "live_simulators", lambda: []):
        with sc.paused("test_bots") as was_running:
            assert sc.running_processes("test_bots") == []

    assert len(was_running) == 3
    print("  без Redis paused работает как раньше")

    print("\n=== 9. непонятный флаг считается ручным запуском ===")
    fake = FakeRedis()
    fake.data[f"{sf.KEY_PREFIX}:general:1"] = "не json"
    # И валидный JSON с полем не того типа: если бы _parse на нём падал,
    # live_simulators вернул бы пустой список — то есть одна кривая запись
    # отключила бы проверку целиком, и скрипт пошёл бы работать.
    fake.data[f"{sf.KEY_PREFIX}:general:2"] = json.dumps(
        {"pid": {"кто-то": "перезаписал"}, "shard": None}
    )
    live = sf.live_simulators(client=fake)
    assert len(live) == 2, "кривая запись не должна прятать остальные"
    assert all(not s.under_supervisor for s in live)
    for s in live:
        print(f"  битое значение -> {s}")

    print("\n=== 10. проверка только для симулятора ===")
    # paused("symbols_history") про флаг ничего не знает: сделки пишет
    # симулятор, а не питатель цен.
    fake_ctl = FakeSupervisorctl(SHARDED)

    with patch.object(sc, "_run", fake_ctl), \
            patch.object(sc, "live_simulators", lambda: manual):
        with sc.paused("candles_history"):
            pass

    print("  чужую программу ручной симулятор не блокирует")

    # А вот отдельный шард — это всё ещё симулятор: под живым ручным
    # процессом данные нельзя менять, сколько бы шардов ни гасили.
    with patch.object(sc, "_run", FakeSupervisorctl(SHARDED)), \
            patch.object(sc, "live_simulators", lambda: manual):
        try:
            with sc.paused("test_bots:test_bots_00"):
                assert False, "блок не должен был начаться"
        except sf.SimulatorIsRunning:
            pass

    print("  а отдельный шард группы — блокирует")

    print("\nOK: симулятор, которого supervisorctl не остановит, "
          "не даст менять данные под собой")


asyncio.run(main())
