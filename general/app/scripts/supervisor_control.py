"""Управление процессами supervisord из скриптов.

Нужно, чтобы безопасно менять то, на что опирается симулятор: список
watched-пар, парк ботов, сам поток цен. Пока симулятор работает, такие
изменения могут смешать разные конфигурации в одном эксперименте. Снимки
price_snapshot:* ограничены сроком от события, но снимок shared_data и
параметры уже открытых сделок сами по себе от этого не обновляются.

Симулятор шардирован: `test_bots` объявлен в supervisord с `numprocs`, и
supervisord поднимает его как группу процессов `test_bots:test_bots_00`,
`test_bots:test_bots_01` и так далее. Само имя `test_bots` для supervisorctl
при этом перестаёт существовать — `supervisorctl stop test_bots` отвечает
`no such process`. Поэтому здесь имя программы везде разворачивается в
реальные имена процессов её группы, а вызывающий код по-прежнему пишет
просто `paused("test_bots")`.

supervisorctl видит только своих детей, поэтому `paused()` дополнительно
смотрит на флаги в Redis (`simulator_flag`): симулятор, запущенный руками,
supervisord не остановит, и работать под ним нельзя — вместо тихой порчи
данных `paused()` бросает `SimulatorIsRunning`.
"""
import logging
import subprocess

from contextlib import contextmanager

from app.scripts.simulator_flag import SimulatorIsRunning, live_simulators

SUPERVISORCTL_TIMEOUT_SECONDS = 60

# Программа, за которой следит флаг в Redis. Проверка нужна только ей: сделки
# пишет симулятор, а не питатель цен или воркеры.
SIMULATOR_PROGRAM = "test_bots"

# Состояния, из которых процесс придёт в RUNNING сам, если его не тронуть:
# STARTING — supervisord только что его запустил, BACKOFF — процесс упал и
# ждёт очередной попытки. Для paused() это «работает»: иначе шард оживёт
# посреди операции, ради которой симулятор и останавливают.
LIVE_STATES = frozenset({"RUNNING", "STARTING", "BACKOFF"})


def _run(*args):
    return subprocess.run(
        ["supervisorctl", *args],
        capture_output=True, text=True,
        timeout=SUPERVISORCTL_TIMEOUT_SECONDS,
    )


def _statuses() -> list[tuple[str, str]]:
    """Пары (имя процесса, состояние) по всему, что знает supervisord.

    Пустой список означает «спросить не удалось»: supervisorctl не найден
    (скрипт запущен вне контейнера) или сам supervisord не отвечает.
    """
    try:
        result = _run("status")
    except Exception as e:
        logging.info(f"supervisorctl недоступен ({e}).")
        return []

    # `supervisorctl status` возвращает ненулевой код, если хоть один процесс
    # не запущен, поэтому смотрим на вывод, а не на код возврата.
    statuses = []

    for line in result.stdout.splitlines():
        parts = line.split()

        if len(parts) >= 2:
            statuses.append((parts[0], parts[1]))

    return statuses


def _statuses_of(program: str) -> list[tuple[str, str]]:
    """Состояния всех процессов программы: и одиночной, и группы с numprocs."""
    return [
        (name, state)
        for name, state in _statuses()
        if name == program or name.startswith(f"{program}:")
    ]


def process_names(program: str) -> list[str]:
    """Имена процессов программы для supervisorctl.

    Для группы с `numprocs` это `test_bots:test_bots_00` и остальные, для
    обычной программы — она сама. Если supervisorctl не ответил, возвращаем
    имя как есть: дальше команда просто не выполнится и напишет об этом
    в лог — то же поведение, что было до шардирования.
    """
    names = [name for name, _ in _statuses_of(program)]

    return names or [program]


def running_processes(program: str) -> list[str]:
    """Имена процессов программы, которые сейчас работают."""
    return [
        name for name, state in _statuses_of(program) if state == "RUNNING"
    ]


def is_running(program: str) -> bool:
    """True, если работает хотя бы один процесс программы. False и при
    отсутствии supervisord (скрипт запущен вне контейнера) — тогда
    останавливать просто нечего."""
    return bool(running_processes(program))


def _report_failure(action: str, name: str, result) -> None:
    """Пишет в лог, если supervisorctl не сделал того, о чём просили.

    Код возврата 0 — сделано, включая «уже остановлен» (`stop` на
    остановленном процессе отвечает `ERROR (not running)`, но кодом 0).
    Ненулевой код — это либо неизвестное имя процесса (1), либо недоступный
    supervisord (4): в обоих случаях процесс нам не подчиняется, и молчать
    об этом нельзя — вызывающий код меняет данные в расчёте на остановленный
    симулятор.
    """
    if not result.returncode:
        return

    message = (result.stdout or "").strip() or (result.stderr or "").strip()

    logging.warning(
        f"supervisorctl {action} {name}: не выполнено "
        f"(код {result.returncode}) — {message}"
    )


def _stop_one(name: str) -> None:
    logging.info(f"Останавливаю {name}...")
    try:
        _report_failure("stop", name, _run("stop", name))
    except Exception as e:
        logging.warning(f"Не удалось остановить {name}: {e}")


def _start_one(name: str) -> None:
    logging.info(f"Запускаю {name}...")
    try:
        _report_failure("start", name, _run("start", name))
    except Exception as e:
        logging.warning(
            f"Не удалось запустить {name}: {e}. "
            f"Сделайте вручную: supervisorctl start {name}"
        )


def _restart_one(name: str) -> None:
    logging.info(f"Перезапускаю {name}...")
    try:
        _report_failure("restart", name, _run("restart", name))
    except Exception as e:
        logging.warning(
            f"Не удалось перезапустить {name}: {e}. "
            f"Сделайте вручную: supervisorctl restart {name}"
        )


def stop(program: str) -> None:
    for name in process_names(program):
        _stop_one(name)


def start(program: str) -> None:
    for name in process_names(program):
        _start_one(name)


def restart(program: str) -> None:
    for name in process_names(program):
        _restart_one(name)


def _refuse_if_simulator_is_unmanaged(
    programs: tuple[str, ...], supervisord_answered: bool
) -> None:
    """Бросает SimulatorIsRunning, если работает симулятор, которого мы не
    остановим.

    Таких случая два.

    Первый — симулятор запущен руками (`python -m app.scripts.start_test_bots`).
    supervisorctl о нём не знает, `stop` его не касается, и вызывающий скрипт
    поменяет данные под живым процессом.

    Второй — supervisorctl не ответил (скрипт запущен вне контейнера). Тогда
    мы не остановили вообще ничего, и любой живой шард — повод отказаться,
    даже если он под supervisord.

    Обратный случай: supervisorctl ответил, и шарды под supervisord мы сейчас
    остановим — их флаги игнорируем. Иначе скрипт отказывался бы работать сам
    из-за себя же: `supervisorctl stop` шлёт SIGTERM, `finally` в процессе не
    выполняется, и флаг висит до TTL уже после остановки.

    Проверка идёт до первого `stop`: незачем гасить парк ради операции, которая
    всё равно не начнётся.
    """
    # Имя группы или конкретного шарда (`test_bots:test_bots_00`): под живым
    # ручным симулятором нельзя менять данные, сколько бы шардов ни гасили.
    if not any(
        program == SIMULATOR_PROGRAM
        or program.startswith(f"{SIMULATOR_PROGRAM}:")
        for program in programs
    ):
        return

    live = live_simulators()

    if supervisord_answered:
        reason = "и supervisorctl его не остановит"
        blocking = [s for s in live if not s.under_supervisor]
    else:
        reason = (
            "а supervisorctl не ответил — остановить его отсюда нечем "
            "(скрипт запущен вне контейнера?)"
        )
        blocking = live

    if not blocking:
        return

    listed = "\n".join(f"  • {s}" for s in blocking)

    raise SimulatorIsRunning(
        f"🚫 Симулятор работает, {reason}:\n{listed}\n"
        f"Остановите его и запустите скрипт снова: в терминале процесса "
        f"введите 'stop' либо `kill <pid>`. Работать под живым симулятором "
        f"нельзя — он продолжит писать сделки по данным, которые вы в этот "
        f"момент меняете.\n"
        f"Если процесса уже нет, флаг исчезнет сам не позже чем через минуту "
        f"(TTL): так ловятся процессы, убитые SIGKILL. Посмотреть флаги — "
        f"python -m app.scripts.simulator_flag"
    )


@contextmanager
def paused(*programs: str):
    """Останавливает процессы на время блока и возвращает как было.

    Запускает обратно только те, что были запущены до входа: если процесс
    намеренно выключен, он таким и останется. Для шардированной программы
    это работает пошардово — выключенный вручную шард не поднимется сам.

        with paused("test_bots"):
            await create_bots()

    Симулятор, запущенный руками, supervisorctl не остановит — на такой
    (`simulator_flag`) блок бросает `SimulatorIsRunning` ещё до первой команды
    `stop`, чтобы вызывающий скрипт не менял данные под живым процессом.

    Команда `stop` идёт всем процессам программы, а не только работающим:
    для уже остановленного это ничего не делает, зато не остаётся состояния,
    из которого процесс поднимется в середине блока (`BACKOFF`, `STARTING`,
    `EXITED` с `autorestart`). Иначе шард переживёт остановку и продолжит
    писать сделки по данным, которые в этот момент меняют.
    """
    statuses = [s for program in programs for s in _statuses_of(program)]

    _refuse_if_simulator_is_unmanaged(
        programs, supervisord_answered=bool(statuses)
    )

    if statuses:
        to_stop = [name for name, _ in statuses]
        was_running = [
            name for name, state in statuses if state in LIVE_STATES
        ]
    else:
        # supervisorctl не ответил — вести себя как раньше: попробовать
        # остановить по имени программы и ничего не поднимать обратно.
        to_stop = list(programs)
        was_running = []

    if was_running:
        logging.info(
            f"⏸  На время операции останавливаю: {', '.join(was_running)}. "
            f"Открытые позиции ботов не будут записаны — они существуют "
            f"только в памяти процесса."
        )

    for name in to_stop:
        _stop_one(name)

    try:
        yield was_running
    finally:
        for name in was_running:
            _start_one(name)
