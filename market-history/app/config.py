from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_URL: str = "postgresql+asyncpg://user:password@postgres:5432/test_db"
    model_config = SettingsConfigDict(env_file="../.env")


settings = Settings()
