import contextlib
import redis

from typing import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class RedisSessionManager:
    __SINGLETON = {}

    def __init__(self, host: str):
        self.pool = redis.asyncio.ConnectionPool.from_url(
            host, decode_responses=True
        )
        self.connection: redis.asyncio.Redis | None = None

    @classmethod
    def create(cls, host: str) -> "RedisSessionManager":
        if host not in cls.__SINGLETON:
            cls.__SINGLETON[host] = cls(host)
        return cls.__SINGLETON[host]

    async def __aenter__(self):
        self.connection = redis.asyncio.Redis.from_pool(
            connection_pool=self.pool
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.connection is not None:
            await self.connection.close()


class DatabaseSessionManager:
    __SINGLETON = {}

    def __init__(self, host):
        self._engine: AsyncEngine = create_async_engine(
            host,
            echo=True,
            query_cache_size=0,
            connect_args={"statement_cache_size": 0},
        )
        self._engine.execution_options(compiled_cache=None)
        self._sessionmaker = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @classmethod
    def create(cls, host: str):
        if host not in cls.__SINGLETON:
            cls.__SINGLETON[host] = cls(host)
        return cls.__SINGLETON[host]

    @contextlib.asynccontextmanager
    async def get_session(self) -> AsyncIterator[AsyncSession]:
        session = self._sessionmaker()

        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
