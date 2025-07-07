from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_URL: str = (
        "postgresql+asyncpg://postgres:secret@localhost:5432/postgres"
    )

    CELERY_BROKER: str = "redis://redis:6379/0"

    model_config = SettingsConfigDict(env_file="../.env")


settings = Settings()
