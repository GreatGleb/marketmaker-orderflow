"""Управление процессами supervisord из скриптов.

Нужно, чтобы безопасно менять то, на что опирается симулятор: список
watched-пар, парк ботов, сам поток цен. Пока симулятор работает, такие
изменения портят данные — в первую очередь потому, что ключи price:* живут
в Redis без TTL: пара выпала из watched_pair, цена замерла, а бот продолжает
«торговать» по ней и писать мусор в test_orders.
"""
import logging
import subprocess

from contextlib import contextmanager

SUPERVISORCTL_TIMEOUT_SECONDS = 60


def _run(*args):
    return subprocess.run(
        ["supervisorctl", *args],
        capture_output=True, text=True,
        timeout=SUPERVISORCTL_TIMEOUT_SECONDS,
    )


def is_running(program: str) -> bool:
    """True, если процесс сейчас работает. False и при отсутствии supervisord
    (скрипт запущен вне контейнера) — тогда останавливать просто нечего."""
    try:
        result = _run("status", program)
    except Exception as e:
        logging.info(
            f"supervisorctl недоступен ({e}) — считаю, что {program} не запущен."
        )
        return False

    return " RUNNING " in result.stdout


def stop(program: str) -> None:
    logging.info(f"Останавливаю {program}...")
    try:
        _run("stop", program)
    except Exception as e:
        logging.info(f"Не удалось остановить {program}: {e}")


def start(program: str) -> None:
    logging.info(f"Запускаю {program}...")
    try:
        _run("start", program)
    except Exception as e:
        logging.info(
            f"Не удалось запустить {program}: {e}. "
            f"Сделайте вручную: supervisorctl start {program}"
        )


def restart(program: str) -> None:
    logging.info(f"Перезапускаю {program}...")
    try:
        _run("restart", program)
    except Exception as e:
        logging.info(
            f"Не удалось перезапустить {program}: {e}. "
            f"Сделайте вручную: supervisorctl restart {program}"
        )


@contextmanager
def paused(*programs: str):
    """Останавливает процессы на время блока и возвращает как было.

    Запускает обратно только те, что были запущены до входа: если процесс
    намеренно выключен, он таким и останется.

        with paused("test_bots"):
            await create_bots()
    """
    was_running = [p for p in programs if is_running(p)]

    if was_running:
        logging.info(
            f"⏸  На время операции останавливаю: {', '.join(was_running)}. "
            f"Открытые позиции ботов не будут записаны — они существуют "
            f"только в памяти процесса."
        )

    for program in was_running:
        stop(program)

    try:
        yield was_running
    finally:
        for program in was_running:
            start(program)
