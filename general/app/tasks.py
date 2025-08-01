from typing import Any, Callable, Optional
import traceback

from celery import Celery
from celery.schedules import crontab

from app.config import settings
from app.exceptions.tasks import TaskNeedsRetry
from app.workers.order_book_cleanup import OrderBookCleaner
from app.workers.clear_old_assets_history import ClearOldAssetsHistoryCommand
from app.sub_services.notifications.factory import NotificationServiceFactory

app = Celery("tasks", broker=settings.CELERY_BROKER)
# app.conf.update(task_always_eager=True)

app.conf.beat_schedule = {
    "daily-order-book-cleanup": {
        "task": "app.tasks.order_book_cleanup_collector",
        "schedule": crontab(minute=0, hour=0),  # Every day at 00:00 UTC
    },
    "clear-old-assets-history-every-day": {
        "task": "app.tasks.clear_old_assets_history",
        "schedule": crontab(minute=0, hour=1),  # 01:00 UTC
    },
}


@app.task(bind=True, default_retry_delay=60, max_retries=3)
def order_book_cleanup_collector(self):
    _run_task(self, OrderBookCleaner, [], lambda x: 0)


@app.task(bind=True, default_retry_delay=60, max_retries=3)
def clear_old_assets_history(self):
    _run_task(self, ClearOldAssetsHistoryCommand, [], lambda x: 0)


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
