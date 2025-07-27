import aiohttp
import asyncio
import logging
from typing import Optional, Dict
from datetime import datetime
from enum import Enum

from app.config import settings

logger = logging.getLogger(__name__)


class NotificationType(Enum):
    """Типы уведомлений"""

    TEST_BOT = "test_bot"
    CELERY_ERROR = "celery_error"


class TelegramNotificationService:
    """
    Сервис для отправки уведомлений в Telegram с поддержкой разных чатов
    """

    def __init__(self, bot_token: str):
        self.bot_token = bot_token
        self.base_url = f"https://api.telegram.org/bot{bot_token}"

    @staticmethod
    def _get_chat_id(notification_type: NotificationType) -> Optional[str]:
        """
        Получает chat_id для указанного типа уведомления

        Args:
            notification_type: Тип уведомления

        Returns:
            chat_id или None если не настроен
        """
        if notification_type == NotificationType.TEST_BOT:
            return settings.TELEGRAM_TEST_BOT_CHAT_ID
        elif notification_type == NotificationType.CELERY_ERROR:
            return settings.TELEGRAM_CELERY_CHAT_ID

        return None

    @staticmethod
    def _get_topic_id(notification_type: NotificationType) -> Optional[str]:
        """
        Получает topic для указанного типа уведомления

        Args:
            notification_type: Тип уведомления

        Returns:
            chat_id или None если не настроен
        """
        if notification_type == NotificationType.TEST_BOT:
            return settings.TELEGRAM_TEST_BOT_TOPIC_ID
        elif notification_type == NotificationType.CELERY_ERROR:
            return settings.TELEGRAM_CELERY_TOPIC_ID

        return None

    async def send_message(
        self,
        message: str,
        chat_id: str,
        message_thread_id: str,
        parse_mode: str = "HTML",
    ) -> bool:
        """
        Отправляет сообщение в Telegram чат

        Args:
            message: Текст сообщения
            chat_id: ID чата для отправки
            message_thread_id: ID topic для отправки
            parse_mode: Режим парсинга (HTML или Markdown)

        Returns:
            bool: True если сообщение отправлено успешно, False в противном случае
        """
        if not chat_id:
            logger.warning("Chat ID не настроен, сообщение не отправлено")
            return False

        try:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/sendMessage"
                data = {
                    "chat_id": chat_id,
                    "message_thread_id": message_thread_id,
                    "text": message,
                    "parse_mode": parse_mode,
                }
                async with session.post(url, json=data) as response:
                    if response.status == 200:
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(
                            f"Ошибка отправки в Telegram чат {chat_id}: {response.status} - {error_text}"
                        )
                        return False

        except Exception as e:
            logger.error(
                f"Ошибка при отправке сообщения в Telegram чат {chat_id}: {e}"
            )
            return False

    async def send_bot_error_notification(
        self,
        bot_id: int,
        error_message: str,
        additional_info: Optional[str] = None,
    ) -> bool:
        """
        Отправляет уведомление об ошибке бота в Telegram

        Args:
            bot_id: ID бота
            error_message: Сообщение об ошибке
            additional_info: Дополнительная информация

        Returns:
            bool: True если уведомление отправлено успешно
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message = f"""
🚨 <b>Ошибка в боте</b>

📊 <b>ID бота:</b> {bot_id}
⏰ <b>Время:</b> {timestamp}
🌍 <b>Environment:</b> {settings.ENVIRONMENT}
❌ <b>Ошибка:</b> {error_message}
"""

        if additional_info:
            message += f"\n📝 <b>Дополнительно:</b> {additional_info}"

        chat_id = self._get_chat_id(NotificationType.TEST_BOT)
        topic_id = self._get_topic_id(NotificationType.TEST_BOT)
        return await self.send_message(
            message=message, chat_id=chat_id, message_thread_id=topic_id
        )

    async def send_celery_error_notification(
        self,
        task_name: str,
        error_message: str,
        additional_info: Optional[str] = None,
    ) -> bool:
        """
        Отправляет уведомление об ошибке Celery в Telegram

        Args:
            task_name: Название задачи Celery
            error_message: Сообщение об ошибке
            additional_info: Дополнительная информация

        Returns:
            bool: True если уведомление отправлено успешно
        """
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message = f"""
🔧 <b>Ошибка Celery задачи</b>

📋 <b>Задача:</b> {task_name}
⏰ <b>Время:</b> {timestamp}
🌍 <b>Environment:</b> {settings.ENVIRONMENT}
❌ <b>Ошибка:</b> {error_message}
"""

        if additional_info:
            message += f"\n📝 <b>Дополнительно:</b> {additional_info}"

        chat_id = self._get_chat_id(NotificationType.CELERY_ERROR)
        topic_id = self._get_topic_id(NotificationType.CELERY_ERROR)
        return await self.send_message(
            message=message, chat_id=chat_id, message_thread_id=topic_id
        )
