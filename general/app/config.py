from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    DB_URL: str = (
        "postgresql+asyncpg://postgres:secret@localhost:5432/postgres"
    )

    CELERY_BROKER: str = "redis://redis:6379/0"

    # Источник рыночных данных: "ws" — поток Binance (как было),
    # "rest" — поллинг /fapi/v1/ticker/24hr (когда push-поток недоступен).
    MARKET_DATA_SOURCE: str = "ws"
    MARKET_DATA_REST_INTERVAL_SEC: float = 2.0

    # spot_ws: по каким парам подписываться.
    #   watched - только пары из watched_pair (по умолчанию)
    #   all     - все спотовые USDT-пары, у которых есть запись
    #             в asset_exchange_specs (остальные писать некуда)
    MARKET_DATA_SPOT_SYMBOLS: str = "watched"
    # spot_ws: какой стрим гонит частоту.
    #   bookTicker - любое изменение лучших bid/ask (~40 мс на ликвидной паре),
    #                last_price пишется как середина спреда (bid+ask)/2
    #   aggTrade   - только реальные сделки (реже), last_price = цена сделки
    MARKET_DATA_SPOT_STREAM: str = "bookTicker"
    # spot_ws: как часто сбрасывать накопленные тики в БД, сек.
    # Тики ловятся все, батч только уменьшает число коммитов.
    MARKET_DATA_SPOT_FLUSH_SEC: float = 0.2
    # Чем помечать источник в asset_history для спотовых котировок.
    MARKET_DATA_SPOT_SOURCE: str = "BINANCE_SPOT"

    ENVIRONMENT: str = "local"

    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    TELEGRAM_TEST_BOT_TOPIC_ID: str = ""
    TELEGRAM_CELERY_TOPIC_ID: str = ""

    model_config = SettingsConfigDict(env_file="../.env")


settings = Settings()
