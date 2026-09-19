"""Симулятор не держит сессию всё время работы процесса.

`Command.run_async` решает зависимости один раз, а `command()` заканчивается
на `await asyncio.gather(*tasks)` — то есть живёт, пока живёт процесс. Сессия,
взятая через `Depends(get_session)`, была бы одна на весь процесс, и открытая
первым же чтением (`get_active_bots`) транзакция висела бы до остановки. Её
снапшот не даёт autovacuum вычистить мёртвые строки, появившиеся после начала
транзакции, то есть ретеншн по `test_orders` удаляет строки, а место не
возвращается. Симулятор запускается шардами, так что таких снапшотов столько,
сколько процессов.

Проверяется на заглушках: сессия берётся из `DatabaseSessionManager` на одну
попытку старта, в паузе ожидания ботов открытых сессий нет, и к моменту
запуска ботов не открыто ни одной. Заодно — что конфиги ботов и `shared_data`
собраны до закрытия сессии и от неё не зависят.

Тот же приём, что в tests/test_worker_session_release.py.
"""
import asyncio
import contextlib
import inspect
from decimal import Decimal
from unittest.mock import patch

import app.bots.demo_test_bot as m
from app.bots.demo_test_bot import StartTestBotsCommand
from app.db.models import TestBot
from tests.strategy_fixtures import execute_strategy_maps

# Сколько раз стартовый цикл не найдёт ботов, прежде чем они появятся.
EMPTY_ATTEMPTS = 2


class FakeSession:
    def __init__(self):
        self.closed = False
        self.rollbacks = 0

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, statement):
        # Симулятор на старте читает карту стратегий той же сессией, что
        # и парк: отдельного похода в базу на это нет.
        return execute_strategy_maps(statement)


class FakeSessionManager:
    """Подмена DatabaseSessionManager: та же форма, без базы."""

    instance = None

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
            session.closed = True

    def open_now(self):
        return [s for s in self.sessions if not s.closed]


class FakeBotCrud:
    """Первые EMPTY_ATTEMPTS попыток парк пуст, потом появляются боты."""

    attempts = 0

    def __init__(self, session):
        self.session = session

    async def get_active_bots(self, shard, shards):
        type(self).attempts += 1

        if type(self).attempts <= EMPTY_ATTEMPTS:
            return []

        return [
            TestBot(id=1, symbol='BMTUSDT', balance=Decimal('1000')),
            TestBot(id=2, symbol='COAIUSDT', balance=Decimal('1000')),
        ]


class FakeMarketDataBuilder:
    built_on = []

    def __init__(self, session):
        self.session = session

    async def build(self):
        # Важно: строится, пока сессия ещё открыта.
        type(self).built_on.append(self.session)
        assert not self.session.closed, (
            'MarketDataBuilder работает на уже закрытой сессии'
        )

        return {'BMTUSDT': {'tick_size': Decimal('0.001')}}


class FakeCache:
    def __init__(self, redis):
        pass

    def start(self):
        pass


class FakeProvider:
    def __init__(self, *args, **kwargs):
        pass


async def main():
    # 1. Сессии и CRUD вообще нет в зависимостях: взять процессную неоткуда.
    params = inspect.signature(StartTestBotsCommand.command).parameters
    assert set(params) == {'self', 'redis'}, (
        f'параметры command(): {sorted(params)} — сессия или CRUD из '
        f'зависимостей живут весь процесс, потому что зависимости решаются '
        f'один раз в Command.run_async'
    )
    print(f"  параметры command(): {sorted(params)}")

    stop_event = asyncio.Event()
    command = StartTestBotsCommand(stop_event=stop_event, shard=0, shards=1)
    manager = FakeSessionManager()
    FakeSessionManager.instance = manager
    FakeBotCrud.attempts = 0
    FakeMarketDataBuilder.built_on = []

    slept = []
    seen_at_start = {}

    async def fake_sleep(seconds):
        slept.append(seconds)

        # Пауза ожидания ботов — единственное место, где известно, что
        # попытка закончилась. Держать в ней соединение незачем.
        open_now = manager.open_now()
        assert not open_now, (
            f'в паузе ожидания открыто сессий: {len(open_now)} — процесс '
            f'держит соединение и транзакцию, пока ботов нет'
        )

    async def fake_simulate_bot(self, **kwargs):
        # Боты пошли: снимаем срез и останавливаем процесс.
        seen_at_start['open'] = len(manager.open_now())
        seen_at_start['config'] = kwargs['original_bot_config']
        seen_at_start['shared_data'] = kwargs['shared_data']
        stop_event.set()

    with patch.object(m, 'DatabaseSessionManager', FakeSessionManager), \
            patch.object(m, 'TestBotCrud', FakeBotCrud), \
            patch.object(m, 'MarketDataBuilder', FakeMarketDataBuilder), \
            patch.object(m, 'PriceCache', FakeCache), \
            patch.object(m, 'PriceProvider', FakeProvider), \
            patch.object(m, 'BinanceBot', FakeProvider), \
            patch.object(m.asyncio, 'sleep', fake_sleep), \
            patch.object(StartTestBotsCommand, 'simulate_bot', fake_simulate_bot):
        await command.command(redis=object())

    # 2. Сессия берётся на попытку: три попытки — три сессии.
    expected = EMPTY_ATTEMPTS + 1
    assert len(manager.sessions) == expected, (
        f'сессий создано {len(manager.sessions)} на {expected} попыток — '
        f'значит одна переиспользуется'
    )
    print(f"  попыток старта: {expected}, сессий создано: "
          f"{len(manager.sessions)}")

    # 3. К моменту запуска ботов не открыто ни одной.
    assert seen_at_start.get('open') == 0, (
        f'на старте ботов открыто сессий: {seen_at_start.get("open")} — '
        f'её транзакция переживёт весь процесс, потому что command() '
        f'заканчивается только на gather'
    )
    print("  на старте ботов открытых сессий: 0")

    assert all(s.closed for s in manager.sessions), 'сессия осталась открытой'
    print(f"  все {len(manager.sessions)} сессий закрыты")

    # 4. shared_data собран, пока сессия была жива (проверка внутри
    #    FakeMarketDataBuilder.build), и на той же сессии, что и боты.
    assert len(FakeMarketDataBuilder.built_on) == 1
    assert FakeMarketDataBuilder.built_on[0] is manager.sessions[-1]
    assert seen_at_start.get('shared_data') == {
        'BMTUSDT': {'tick_size': Decimal('0.001')}
    }
    print("  shared_data собран на живой сессии и доехал до ботов")

    # 5. Конфиг бота — namedtuple со значениями, а не ORM-объект,
    #    привязанный к закрытой сессии.
    config = seen_at_start.get('config')
    assert hasattr(config, '_fields'), (
        f'боту уехал {type(config).__name__}, а не namedtuple: он держал бы '
        f'ссылку на сессию, которой уже нет'
    )
    assert config.id == 1 and config.symbol == 'BMTUSDT', config
    print(f"  конфиг бота: {type(config).__name__}, id={config.id}, "
          f"symbol={config.symbol}")

    # 6. Смысл ожидания не потерян: паузы были ровно на пустых попытках.
    waits = [s for s in slept if s == 60]
    assert len(waits) == EMPTY_ATTEMPTS + 1, (
        f'пауз по 60 с: {len(waits)}, ждали {EMPTY_ATTEMPTS + 1} '
        f'(стартовая плюс по одной на пустую попытку)'
    )
    print(f"  пауз по 60 с: {len(waits)}")

    print("\nвсё сошлось")


if __name__ == "__main__":
    asyncio.run(main())
