from typing import Any, Generic, Optional, Type, TypeVar
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import UnaryExpression, func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")


class PaginateResponse(BaseModel):
    total_count: int = 0
    count: int = 0
    page_count: int = 0
    data: Any


class BaseCrud(Generic[T]):
    def __init__(self, session: AsyncSession, entity_type: Type[T]) -> None:
        self.session = session
        self.entity_type = entity_type

    async def persist(self, model: T) -> T:
        if model.id is None:
            self.session.add(model)

        await self.session.flush()
        return model

    async def delete(self, model: T) -> T:
        await self.session.delete(model)
        await self.session.flush()
        return model

    async def find_by_id(self, id_: str | UUID) -> T | None:
        query = await self.session.scalars(
            select(self.entity_type).where(self.entity_type.id == id_)
        )
        return query.one_or_none()

    async def count(self) -> int:
        query = select(func.count(self.entity_type.id))
        result = await self.session.execute(query)

        return int(result.scalar_one())

    async def all(
        self,
        limit: int | None = None,
        offset: int | None = None,
        order_by: UnaryExpression | None = None,
        options: list | None = None,
    ):
        if options is None:
            options = []
        stmt = select(self.entity_type).options(*options)

        if order_by is not None:
            stmt = stmt.order_by(order_by)
        if limit:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)

        result = await self.session.scalars(stmt)
        return result.unique().all()

    async def get_paginated_data(
        self,
        stmt,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        is_scalars: bool = False,
    ):

        count_stmt = select(func.count()).select_from(stmt.subquery())
        count_result = await self.session.execute(count_stmt)
        total_count = count_result.scalar()

        if limit:
            stmt = stmt.limit(limit)
        if offset:
            stmt = stmt.offset(offset)

        result = await self.session.execute(stmt)
        items = result.scalars().all() if is_scalars else result.all()

        page_count = 0
        if total_count is not None and limit is not None and limit > 0:
            page_count = (total_count + limit - 1) // limit

        return PaginateResponse(
            count=len(items),
            page_count=page_count,
            data=items,
            total_count=total_count,
        )
