"""Флаг «симулятор работает» в Redis.

**Зачем.** Пересборка `watched_pair` и парка ботов идёт под остановленным
симулятором: `supervisor_control.paused()` гасит его процессы через
`supervisorctl` и поднимает обратно. Но симулятор, запущенный руками
(`python -m app.scripts.start_test_bots`), supervisord не подчиняется —
`supervisorctl` о нём не знает, `paused()` его не остановит и молча вернёт
управление. Дальше `TRUNCATE` в `new_bots.py` сносит `test_bots` вместе с
`test_orders`, а живой процесс продолжает писать сделки от имени уже
несуществующих ботов; при пересборке `watched_pair` — торговать по паре,
котировки по которой больше не приходят.

Поэтому каждый процесс симулятора на старте поднимает в Redis свой флаг, а
скрипты перед опасной операцией смотрят, не остался ли процесс, которого
supervisord не остановит.

**Ключ на процесс, а не счётчик.** Симулятор шардирован: процессов столько,
сколько стоит в `TEST_BOTS_SHARDS`. Счётчик (`INCR` на старте, `DECR` на
выходе) течёт — процесс, убитый `SIGKILL`, свой `DECR` не сделает, и счётчик
навсегда останется больше нуля, то есть заблокирует все скрипты насовсем.
Ключ на процесс с TTL такого не может: он живёт, только пока процесс его
продлевает, и через `TTL_SECONDS` после смерти процесса исчезает сам.

В ключе `host` и `pid`, а не номер шарда: ручной запуск поверх supervisord —
это как раз два процесса с одинаковым `--shard`, и по номеру шарда они
затирали бы флаги друг друга (а выход одного снимал бы флаг второго, ещё
живого). `host` нужен для запуска из другого контейнера или с другой машины
на общий Redis.

**Ручной запуск от supervisord отличается по окружению.** supervisord кладёт
своим детям `SUPERVISOR_PROCESS_NAME`; процесс записывает это имя в значение
флага. Скриптам блокироваться на шардах supervisord не нужно — их останавливает
`paused()`, и после `supervisorctl stop` их флаги ещё висят до TTL (`SIGTERM`
не даёт выполниться `finally`). Блокирует только флаг без имени процесса
supervisord, то есть ручной запуск.

**Чего флаг не умеет.**

* Процесс, убитый `SIGKILL` или `SIGTERM`, флаг за собой не удалит: `finally`
  в этих случаях не выполняется. Флаг проживёт ещё до `TTL_SECONDS`, и всё это
  время скрипты будут отказываться работать, хотя симулятора уже нет. Лечится
  ожиданием минуты или `redis-cli del <ключ>` — ключи покажет
  `python -m app.scripts.simulator_flag`.
* Если Redis недоступен, флаг ни поставить, ни прочитать. Скрипты в этом
  случае пишут предупреждение и работают дальше — так же, как при недоступном
  `supervisorctl` (`supervisor_control._statuses`). Симулятор без Redis всё
  равно не торгует: цены он берёт оттуда же.

Посмотреть, кто сейчас держит флаг:

    python -m app.scripts.simulator_flag
"""
import asyncio
import json
import logging
import os
import socket

from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone

import redis

from app.dependencies import REDIS_URL

UTC = timezone.utc

KEY_PREFIX = "test_bots_running"

# Сколько флаг живёт без продления. Верхняя граница окна, в котором скрипт
# считает работающим уже мёртвый процесс, и одновременно запас на то, что
# событийный цикл симулятора занят: под полной нагрузкой (7 тысяч ботов с
# тактом 100 мс) задача продления просыпается не секунда в секунду.
TTL_SECONDS = 60
# Продлеваем вчетверо чаще, чем истекает: три подряд пропущенных продления
# (например, Redis моргнул) флаг ещё переживёт.
REFRESH_INTERVAL_SECONDS = 15

# Ждать ответа Redis в скриптах дольше нет смысла: это одна короткая команда,
# и если её не с кем выполнить, лучше сразу написать об этом и не держать
# человека у консоли.
CONNECT_TIMEOUT_SECONDS = 3


class SimulatorIsRunning(RuntimeError):
    """Опасную операцию нельзя выполнять: симулятор работает.

    Бросается из `supervisor_control.paused()`. Точки входа скриптов ловят её
    и выходят с кодом 1: текст исключения — готовое сообщение человеку.
    """


@dataclass(frozen=True)
class Simulator:
    """Один процесс симулятора, поднявший флаг."""

    key: str
    host: str
    pid: int
    shard: int
    shards: int
    # Имя процесса в supervisord или "" — для запущенного руками.
    supervisor: str
    started_at: str

    @property
    def under_supervisor(self) -> bool:
        return bool(self.supervisor)

    def __str__(self) -> str:
        where = (
            f"supervisord/{self.supervisor}" if self.supervisor
            else "запущен вручную"
        )

        return (
            f"шард {self.shard}/{self.shards}, {where}, "
            f"{self.host} pid {self.pid}, с {self.started_at}"
        )


def own_key() -> str:
    return f"{KEY_PREFIX}:{socket.gethostname()}:{os.getpid()}"


def _own_value(shard: int, shards: int) -> str:
    return json.dumps(
        {
            "host": socket.gethostname(),
            "pid": os.getpid(),
            "shard": shard,
            "shards": shards,
            # supervisord кладёт это имя всем своим детям. Пусто — значит
            # процесс запущен руками, и supervisorctl его не остановит.
            "supervisor": os.environ.get("SUPERVISOR_PROCESS_NAME", ""),
            "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    )


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse(key: str, raw: str) -> Simulator:
    """Значение флага -> Simulator.

    Битое или чужое значение считаем ручным запуском: непонятный флаг должен
    останавливать операцию, а не проскакивать мимо проверки. Поэтому здесь
    ничего не бросается — исключение поймал бы `live_simulators`, вернул бы
    пустой список, и одна кривая запись отключила бы проверку целиком.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        data = {}

    if not isinstance(data, dict):
        data = {}

    return Simulator(
        key=key,
        host=str(data.get("host", "?")),
        pid=_as_int(data.get("pid")),
        shard=_as_int(data.get("shard")),
        shards=_as_int(data.get("shards")),
        supervisor=str(data.get("supervisor", "")),
        started_at=str(data.get("started_at", "?")),
    )


def live_simulators(client=None) -> list[Simulator]:
    """Процессы симулятора, чей флаг сейчас жив.

    Пустой список — это и «никто не работает», и «спросить не удалось»:
    во втором случае в лог уходит предупреждение. Разделять их отдельным
    исключением незачем — недоступный Redis означает, что симулятор всё равно
    не торгует, а скрипт останавливать не на чем.

    `client` — готовое синхронное соединение; нужен проверкам, обычный вызов
    открывает своё и закрывает за собой.
    """
    own_client = None

    if client is None:
        own_client = client = redis.Redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=CONNECT_TIMEOUT_SECONDS,
            socket_timeout=CONNECT_TIMEOUT_SECONDS,
        )

    try:
        found = []

        # SCAN, а не KEYS: в этом Redis лежат цены, очередь сделок и конфиги
        # копиботов — тысячи ключей, и KEYS заблокировал бы его целиком.
        for key in client.scan_iter(match=f"{KEY_PREFIX}:*", count=100):
            raw = client.get(key)

            # Истёк между scan и get — значит процесса уже нет.
            if raw is None:
                continue

            found.append(_parse(key, raw))

        return sorted(found, key=lambda s: (s.shard, s.host, s.pid))
    except Exception as e:
        logging.warning(
            f"Не удалось проверить в Redis, работает ли симулятор ({e}). "
            f"Продолжаю, но если он запущен вручную — остановите его сами: "
            f"иначе он будет писать сделки поверх меняющихся данных."
        )
        return []
    finally:
        if own_client is not None:
            with suppress(Exception):
                own_client.close()


async def _touch(client, key: str, value: str) -> bool:
    try:
        await client.set(key, value, ex=TTL_SECONDS)
        return True
    except Exception as e:
        logging.warning(
            f"Не удалось записать флаг {key} в Redis ({e}). Если он так и не "
            f"появится, new_bots и seed_watched_pairs не увидят этот процесс "
            f"и смогут менять данные под ним."
        )
        return False


async def _keep_alive(client, key: str, value: str) -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        await _touch(client, key, value)


@asynccontextmanager
async def hold(shard: int = 0, shards: int = 1, client=None):
    """Держит флаг этого процесса, пока идёт блок.

        async with simulator_flag.hold(shard=0, shards=4):
            await asyncio.gather(*tasks)

    Ставит ключ сразу, дальше продлевает его фоновой задачей и снимает на
    выходе — в том числе если блок кончился исключением или отменой (Ctrl+C).
    Сбой Redis блок не роняет: симулятор из-за него останавливать нечестно,
    а флаг вернётся на следующем продлении.
    """
    key = own_key()
    value = _own_value(shard, shards)

    own_client = None

    if client is None:
        # Своё соединение, а не общий пул из RedisSessionManager: пул —
        # синглтон, который закрывает первый же вышедший из него потребитель,
        # а флаг должен жить ровно столько, сколько процесс.
        own_client = client = redis.asyncio.Redis.from_url(
            REDIS_URL, decode_responses=True
        )

    await _touch(client, key, value)

    task = asyncio.create_task(_keep_alive(client, key, value))

    try:
        yield key
    finally:
        task.cancel()

        with suppress(asyncio.CancelledError):
            await task

        try:
            await client.delete(key)
        except Exception as e:
            logging.warning(
                f"Не удалось снять флаг {key} ({e}). Сам исчезнет через "
                f"{TTL_SECONDS} с."
            )

        if own_client is not None:
            with suppress(Exception):
                await own_client.aclose()


def main() -> int:
    logging.basicConfig(format="%(message)s", level=logging.INFO)

    simulators = live_simulators()

    if not simulators:
        print("Флагов нет: ни один процесс симулятора не отчитывается.")
        return 0

    print(f"Симуляторов с живым флагом: {len(simulators)}")

    for simulator in simulators:
        print(f"  {simulator.key}  —  {simulator}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
