import redis

from functools import lru_cache, partial
from typing import Annotated, Type, TypeVar

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette import status

from app.config import Settings, settings
from app.db.base import DatabaseSessionManager, RedisSessionManager


async def get_session():
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]

CrudT = TypeVar("CrudT")


async def get_redis() -> redis.asyncio.client.Redis:
    rsm = RedisSessionManager.create("redis://:@redis:6379/0")
    async with rsm as r:
        yield r.connection


@lru_cache
def get_settings():
    return Settings()


def get_crud(crud_type: Type[CrudT], session: SessionDep) -> CrudT:
    return crud_type(session)


def resolve_crud(crud_type: Type[CrudT]) -> CrudT:
    return Depends(partial(get_crud, crud_type))


async def api_key_auth(x_api_key: str = Header(...)):
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid or missing API Key",
    )
