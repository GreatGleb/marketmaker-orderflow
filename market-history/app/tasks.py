from typing import Any, Callable, Optional

from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.exceptions.tasks import TaskNeedsRetry
from app.workers.order_book_cleanup import OrderBookCleaner


app = Celery("tasks", broker=settings.CELERY_BROKER)
# app.conf.update(task_always_eager=True)

app.conf.beat_schedule = {
    "daily-order-book-cleanup": {
        "task": "app.tasks.order_book_cleanup_collector",
        "schedule": crontab(minute="*/2"),  # Every day at 00:00 UTC
    },
}


@app.task(bind=True, default_retry_delay=60, max_retries=3)
def order_book_cleanup_collector(self):
    _run_task(self, OrderBookCleaner, [], lambda x: 0)


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
    except Exception:
        if self.request.retries >= 25:
            raise self.retry(
                args=args,
                countdown=countdown(self.request.retries),
                max_retries=4,
            )
        raise self.retry(args=args, countdown=countdown(self.request.retries))
