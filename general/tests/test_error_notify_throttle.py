"""Проверка троттлинга уведомлений об ошибках ботов.

При системном сбое в одну и ту же ошибку утыкаются сразу все боты. Без
троттлинга это тысячи сообщений в Telegram в минуту: бота забанят, а причину
сбоя в такой лавине не найти. Здесь следим, что за интервал уходит ровно одно
сообщение, а подавленные учтены в его тексте.
"""
import asyncio
from unittest.mock import patch

import app.bots.demo_test_bot as m

Command = m.StartTestBotsCommand


class FakeTelegram:
    def __init__(self):
        self.sent = []

    async def send_bot_error_notification(self, bot_id, error_message, additional_info=None):
        self.sent.append((bot_id, error_message, additional_info))
        return True


def reset():
    Command._last_error_notification.clear()
    Command._suppressed_error_notifications.clear()


async def main():
    print("=== 1. лавина одинаковых ошибок = одно сообщение ===")
    reset()
    fake = FakeTelegram()

    with patch.object(
        m.NotificationServiceFactory, "get_telegram_service", return_value=fake
    ):
        for bot_id in range(1, 1001):
            await Command._notify_bot_error(
                bot_id=bot_id,
                error=RuntimeError("Redis недоступен"),
                error_traceback="stack",
            )

    print(f"  ботов упало 1000, сообщений ушло {len(fake.sent)}")
    assert len(fake.sent) == 1
    assert Command._suppressed_error_notifications[
        "RuntimeError: Redis недоступен"
    ] == 999

    print("\n=== 2. разные ошибки не глушат друг друга ===")
    with patch.object(
        m.NotificationServiceFactory, "get_telegram_service", return_value=fake
    ):
        await Command._notify_bot_error(
            bot_id=1, error=ValueError("Redis недоступен"), error_traceback="stack"
        )
        await Command._notify_bot_error(
            bot_id=1, error=RuntimeError("нет цены"), error_traceback="stack"
        )

    print(f"  всего сообщений: {len(fake.sent)}")
    assert len(fake.sent) == 3

    print("\n=== 3. после интервала уходит сообщение со счётчиком ===")
    with patch.object(
        m.NotificationServiceFactory, "get_telegram_service", return_value=fake
    ), patch.object(Command, "ERROR_NOTIFY_INTERVAL_SECONDS", 0.05):
        await asyncio.sleep(0.06)
        await Command._notify_bot_error(
            bot_id=7,
            error=RuntimeError("Redis недоступен"),
            error_traceback="stack",
        )

    _, _, additional_info = fake.sent[-1]
    print(f"  {additional_info.splitlines()[0]}")
    assert "999" in additional_info and "Полный стек ошибки" in additional_info
    # Счётчик обнулён: следующее сообщение не должен повторять старое число.
    assert "RuntimeError: Redis недоступен" not in Command._suppressed_error_notifications

    print("\n=== 4. переполнение словаря чистит просроченные ключи ===")
    reset()
    with patch.object(
        m.NotificationServiceFactory, "get_telegram_service", return_value=fake
    ), patch.object(Command, "ERROR_NOTIFY_INTERVAL_SECONDS", 0.01), \
            patch.object(Command, "ERROR_NOTIFY_MAX_KEYS", 50):
        for i in range(200):
            await Command._notify_bot_error(
                bot_id=1,
                error=RuntimeError(f"ошибка с меняющимся id {i}"),
                error_traceback="stack",
            )
            await asyncio.sleep(0.001)

    print(f"  ключей в памяти: {len(Command._last_error_notification)}")
    assert len(Command._last_error_notification) <= Command.ERROR_NOTIFY_MAX_KEYS

    print("\n=== 5. падение Telegram не роняет вызывающего ===")
    reset()

    class BrokenTelegram:
        async def send_bot_error_notification(self, **kwargs):
            raise RuntimeError("Telegram недоступен")

    with patch.object(
        m.NotificationServiceFactory,
        "get_telegram_service",
        return_value=BrokenTelegram(),
    ):
        await Command._notify_bot_error(
            bot_id=1, error=RuntimeError("что-то"), error_traceback="stack"
        )

    print("  исключение проглочено")

    reset()
    print("\nOK: лавина уведомлений подавлена, счётчик и память под контролем")

asyncio.run(main())
