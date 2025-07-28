from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_URL: str = (
        "postgresql+asyncpg://postgres:secret@localhost:5432/postgres"
    )

    CELERY_BROKER: str = "redis://redis:6379/0"

    ENVIRONMENT: str = "local"

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    TELEGRAM_TEST_BOT_TOPIC_ID: str = ""
    TELEGRAM_CELERY_TOPIC_ID: str = ""

    model_config = SettingsConfigDict(env_file="../.env")


settings = Settings()
