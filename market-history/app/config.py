from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_URL: str = (
        "postgresql+asyncpg://postgres:secret@localhost:5432/postgres"
    )
    model_config = SettingsConfigDict(env_file="../.env")


settings = Settings()
