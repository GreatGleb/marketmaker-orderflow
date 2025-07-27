from typing import Optional
from app.config import settings
from app.sub_services.notifications.telegram_service import (
    TelegramNotificationService,
)


class NotificationServiceFactory:
    """
    Фабрика для создания сервисов уведомлений
    """

    _telegram_service: Optional[TelegramNotificationService] = None

    @classmethod
    def get_telegram_service(cls) -> Optional[TelegramNotificationService]:
        """
        Возвращает экземпляр TelegramNotificationService

        Returns:
            TelegramNotificationService или None если не настроены токены
        """
        if cls._telegram_service is None:
            if settings.TELEGRAM_BOT_TOKEN:
                cls._telegram_service = TelegramNotificationService(
                    bot_token=settings.TELEGRAM_BOT_TOKEN,
                )
            else:
                return None

        return cls._telegram_service

    @classmethod
    def reset_telegram_service(cls):
        """
        Сбрасывает кэшированный экземпляр сервиса
        """
        cls._telegram_service = None
