"""Адрес Redis берётся из настроек, а не из кода.

Раньше `redis://:@redis:6379/0` был константой в `app/dependencies.py`, и
запуск вне docker-сети требовал правки файла (пункт 4.6 в
09-roadmap.md, пункт 15 в 08-gotchas.md). Здесь проверяется, что переменная
окружения действительно доходит до всех трёх потребителей и что брокер
celery по умолчанию смотрит туда же, куда всё остальное.
"""
import logging
import os
from unittest.mock import patch

from app import dependencies
from app.config import Settings
from app.scripts import simulator_flag

OTHER = "redis://localhost:6380/1"


def env(**overrides):
    """Настройки, собранные с нуля на подменённом окружении.

    `Settings()` читает `os.environ`, поэтому важно убрать и то, что уже
    лежит в окружении контейнера, — иначе тест проверял бы .env, а не код.
    """
    clean = {
        k: v
        for k, v in os.environ.items()
        if k not in {"REDIS_URL", "CELERY_BROKER"}
    }
    clean.update(overrides)

    with patch.dict(os.environ, clean, clear=True):
        return Settings()


def test_url_comes_from_env():
    s = env(REDIS_URL=OTHER)

    assert s.REDIS_URL == OTHER, "REDIS_URL из окружения не подхватился"
    print(f"  REDIS_URL из окружения: {s.REDIS_URL}")


def test_default_is_docker_service():
    s = env()

    assert s.REDIS_URL == "redis://redis:6379/0", (
        f"умолчание изменилось: {s.REDIS_URL}. Внутри docker-compose это "
        f"имя сервиса, менять его нельзя без правки compose"
    )
    print(f"  умолчание: {s.REDIS_URL}")


def test_celery_broker_follows_redis():
    """Пустой брокер — это общий Redis, а не пустая строка."""
    s = env(REDIS_URL=OTHER)

    assert s.CELERY_BROKER == OTHER, (
        f"брокер не подхватил REDIS_URL: {s.CELERY_BROKER!r}. Так при "
        f"переезде Redis celery молча остался бы на старом адресе"
    )

    s = env(REDIS_URL=OTHER, CELERY_BROKER="")
    assert s.CELERY_BROKER == OTHER, "пустая строка в .env не сработала"

    own = "redis://broker:6379/3"
    s = env(REDIS_URL=OTHER, CELERY_BROKER=own)
    assert s.CELERY_BROKER == own, "явный брокер затёрло общим Redis"

    print(f"  брокер без настройки: {env(REDIS_URL=OTHER).CELERY_BROKER}")
    print(f"  брокер заданный явно: {own}")


def test_celery_prefers_env_over_argument():
    """Почему `app/tasks.py` вообще трогает окружение.

    Celery 5.5 разрешает адрес как `os.environ.get("CELERY_BROKER_URL") or
    conf.first("broker_url", ...)`, то есть переменная окружения выигрывает у
    аргумента `broker=`. Проверка ждёт именно этого — если в новой версии
    celery поведение изменится, тест скажет, и перезапись в `tasks.py` можно
    будет убрать.
    """
    from celery import Celery

    trap = "redis://from-env:6379/9"

    with patch.dict(os.environ, {"CELERY_BROKER_URL": trap}):
        bare = Celery("проверка", broker=OTHER)

        assert bare.conf.broker_url == trap, (
            "celery перестал предпочитать CELERY_BROKER_URL — перезапись "
            "окружения в app/tasks.py больше не нужна"
        )

    print(f"  голый Celery(broker={OTHER}) при env берёт {trap}")


def test_celery_app_uses_the_setting():
    """Настройка сильнее окружения — даже забытого в `.env` на сервере.

    `general/.env` лежит в `.gitignore`, у каждого сервера он свой, и старая
    строка `CELERY_BROKER_URL` там переживёт выкатку. Раньше она молча
    перебивала `settings.CELERY_BROKER`; теперь `app/tasks.py` приводит
    окружение к настройке.
    """
    import importlib

    from app import config, tasks

    stale = "redis://из-старого-env:6379/9"

    try:
        with patch.object(config.settings, "CELERY_BROKER", OTHER):
            with patch.dict(
                os.environ,
                {
                    "CELERY_BROKER_URL": stale,
                    "CELERY_RESULT_BACKEND": stale,
                },
            ):
                reloaded = importlib.reload(tasks)

                broker = reloaded.app.conf.broker_url
                assert broker == OTHER, (
                    f"брокер celery {broker!r} вместо {OTHER!r} — строка из "
                    f"старого .env снова перебивает настройку"
                )

                backend = reloaded.app.conf.result_backend
                assert backend == OTHER, f"backend не там же: {backend}"

                print(f"  celery собран на {broker}")
                print(f"  протухшее {stale} в окружении проигнорировано")
    finally:
        importlib.reload(tasks)


def test_no_hardcoded_url_left():
    """Ни один потребитель не держит свой адрес."""
    import inspect

    for module in (dependencies, simulator_flag):
        src = inspect.getsource(module)

        assert "redis://" not in src, (
            f"в {module.__name__} остался адрес Redis в коде — вне "
            f"docker-сети он опять не подключится"
        )

    print("  адресов в dependencies.py и simulator_flag.py нет")


def test_consumers_read_settings():
    """`get_redis`, `redis_context` и флаг симулятора берут один и тот же URL.

    Пул в `RedisSessionManager` — синглтон по адресу: разъехавшиеся
    потребители получили бы разные пулы к разным серверам и не увидели бы
    ключей друг друга.
    """
    created = []

    class FakeManager:
        connection = object()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def fake_create(host):
        created.append(host)
        return FakeManager()

    import asyncio

    async def drain():
        with patch.object(dependencies.settings, "REDIS_URL", OTHER):
            with patch(
                "app.dependencies.RedisSessionManager.create", fake_create
            ):
                async for _ in dependencies.get_redis():
                    break

                async with dependencies.redis_context():
                    pass

    asyncio.run(drain())

    assert created == [OTHER, OTHER], f"разные адреса: {created}"

    # Флаг симулятора держит своё соединение (пул-синглтон закрывает первый
    # же вышедший потребитель), но адрес должен быть тот же.
    from_url = []

    # Заглушка возвращает None вместо клиента, и `live_simulators` пишет об
    # этом в лог — в выводе теста это лишняя строка про остановку симулятора.
    logging.disable(logging.WARNING)

    def stub(url, **kwargs):
        from_url.append(url)

    try:
        with patch.object(simulator_flag.settings, "REDIS_URL", OTHER):
            with patch("redis.Redis.from_url", stub):
                simulator_flag.live_simulators()
    finally:
        logging.disable(logging.NOTSET)

    assert from_url == [OTHER], f"simulator_flag взял другой адрес: {from_url}"
    print(f"  все потребители открывают {OTHER}")


if __name__ == "__main__":
    test_url_comes_from_env()
    test_default_is_docker_service()
    test_celery_broker_follows_redis()
    test_celery_prefers_env_over_argument()
    test_celery_app_uses_the_setting()
    test_no_hardcoded_url_left()
    test_consumers_read_settings()
    print("\nOK")
