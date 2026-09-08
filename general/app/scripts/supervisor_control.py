"""Управление процессами supervisord из скриптов.

Нужно, чтобы безопасно менять то, на что опирается симулятор: список
watched-пар, парк ботов, сам поток цен. Пока симулятор работает, такие
изменения портят данные — в первую очередь потому, что ключи price:* живут
в Redis без TTL: пара выпала из watched_pair, цена замерла, а бот продолжает
«торговать» по ней и писать мусор в test_orders.

Симулятор шардирован: `test_bots` объявлен в supervisord с `numprocs`, и
supervisord поднимает его как группу процессов `test_bots:test_bots_00`,
`test_bots:test_bots_01` и так далее. Само имя `test_bots` для supervisorctl
при этом перестаёт существовать — `supervisorctl stop test_bots` отвечает
`no such process`. Поэтому здесь имя программы везде разворачивается в
реальные имена процессов её группы, а вызывающий код по-прежнему пишет
просто `paused("test_bots")`.
"""
import logging
import subprocess

from contextlib import contextmanager

SUPERVISORCTL_TIMEOUT_SECONDS = 60

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


@contextmanager
def paused(*programs: str):
    """Останавливает процессы на время блока и возвращает как было.

    Запускает обратно только те, что были запущены до входа: если процесс
    намеренно выключен, он таким и останется. Для шардированной программы
    это работает пошардово — выключенный вручную шард не поднимется сам.

        with paused("test_bots"):
            await create_bots()

    Команда `stop` идёт всем процессам программы, а не только работающим:
    для уже остановленного это ничего не делает, зато не остаётся состояния,
    из которого процесс поднимется в середине блока (`BACKOFF`, `STARTING`,
    `EXITED` с `autorestart`). Иначе шард переживёт остановку и продолжит
    писать сделки по данным, которые в этот момент меняют.
    """
    statuses = [s for program in programs for s in _statuses_of(program)]

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
