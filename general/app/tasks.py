import os
import traceback
from typing import Any, Callable, Optional

from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.exceptions.tasks import TaskNeedsRetry
from app.workers.retention import RetentionCommand
from app.workers.test_order_rollup import TestOrderRollupCommand
from app.sub_services.notifications.factory import NotificationServiceFactory

# Celery 5.5 разрешает адрес как
# `os.environ.get("CELERY_BROKER_URL") or conf.first("broker_url", ...)`
# (`celery/app/utils.py`, свойства `broker_url` и `result_backend`), то есть
# переменная окружения выигрывает у аргументов `broker=` и `backend=`. Так
# `settings.CELERY_BROKER` уже один раз молча не применялся: `.env` и
# docker-compose ставили `CELERY_BROKER_URL` с тем же адресом, и подмены
# было не видно, пока адрес не разъехался.
#
# Поэтому переменные не «не ставим», а приводим к настройке: `general/.env`
# лежит в `.gitignore`, на каждом сервере он свой, и забытая там строка
# иначе продолжала бы тихо перебивать `REDIS_URL`. Единственный источник
# адреса — `settings.CELERY_BROKER` (по умолчанию — `REDIS_URL`); менять
# брокер надо им, а не этими переменными.
os.environ["CELERY_BROKER_URL"] = settings.CELERY_BROKER
os.environ["CELERY_RESULT_BACKEND"] = settings.CELERY_BROKER

app = Celery(
    "tasks",
    broker=settings.CELERY_BROKER,
    backend=settings.CELERY_BROKER,
)
# app.conf.update(task_always_eager=True)

# Чистка идёт каждый час, а не раз в сутки. Суточный проход означал бы, что к
# ночи накопилось 13 ГБ и 42 миллиона строк, которые надо снести за один раз:
# такой DELETE раздувает WAL и держит базу, а autovacuum за ним не успевает.
# Часовой проход сносит примерно 1/24 этого объёма и проходит незаметно.
app.conf.beat_schedule = {
    "retention-hourly": {
        "task": "app.tasks.retention",
        "schedule": crontab(minute=20),  # каждый час в :20
    },
    # Свёртки должны опережать чистку: `test_orders` удаляется только до
    # границы свёрнутого, и если свёртки встанут, чистка встанет следом.
    "test-order-rollup": {
        "task": "app.tasks.test_order_rollup",
        "schedule": crontab(minute="*/10"),
    },
}


@app.task(bind=True, default_retry_delay=60, max_retries=3)
def retention(self):
    _run_task(self, RetentionCommand, [], get_countdown)


@app.task(bind=True, default_retry_delay=60, max_retries=3)
def test_order_rollup(self):
    _run_task(self, TestOrderRollupCommand, [], get_countdown)


def get_countdown(retry: int) -> int:
    if retry < 15:
        return (
            60 * 1
        )  # during first 15 minutes - 15 attempts (1 attempt / 1 minute)

    if retry <= 24:
        return (
            60 * 5
        )  # during next 2 hours - 10 attempts (1 attempt / 5 minutes)

    return 60 * 60 * 12  # next - 1 attempt / 12 hours


def _run_task(
    self,
    cmd: type,
    args: list[Any],
    countdown: Callable[[int], int],
    success_callback: Optional[Callable] = None,
):
    try:
        result = cmd(*args).run()
        if result.need_retry:
            raise TaskNeedsRetry()

        if result.success and success_callback is not None:
            success_callback()
    except Exception as e:
        try:
            telegram_service = (
                NotificationServiceFactory.get_telegram_service()
            )
            if telegram_service:
                error_traceback = traceback.format_exc()
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        telegram_service.send_celery_error_notification(
                            task_name=self.request.task,
                            error_message=str(e),
                            additional_info=f"Полный стек ошибки:\n{error_traceback}\n\nПопытка: {self.request.retries + 1}",
                        )
                    )
                finally:
                    loop.close()
        except Exception as telegram_error:
            print(
                f"❌ Ошибка при отправке уведомления Celery в Telegram: {telegram_error}"
            )

        if self.request.retries >= 25:
            raise self.retry(
                args=args,
                countdown=countdown(self.request.retries),
                max_retries=4,
            )
        raise self.retry(args=args, countdown=countdown(self.request.retries))
