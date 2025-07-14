from decimal import Decimal

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from typing import Optional

from app.db.models import TestOrder
from app.crud.base import BaseCrud


class TestOrderCrud(BaseCrud[TestOrder]):

    def __init__(self, session: AsyncSession):
        super().__init__(session, TestOrder)

    async def create(self, data: dict):
        stmt = insert(TestOrder).values(data)
        await self.session.execute(stmt)

    async def bulk_create(self, orders):
        stmt = insert(TestOrder).values(orders)
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_active_by_symbol(self, symbol: str) -> Optional[TestOrder]:
        stmt = (
            select(TestOrder)
            .where(TestOrder.asset_symbol == symbol)
            .where(TestOrder.is_active == True)  # noqa
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def close_order(
        self,
        order_id: int,
        close_price: Decimal,
        close_time,
        close_fee: float,
        profit_loss: Decimal,
    ) -> None:
        stmt = (
            update(TestOrder)
            .where(TestOrder.id == order_id)
            .values(
                close_price=close_price,
                close_time=close_time,
                close_fee=close_fee,
                profit_loss=profit_loss,
                is_active=False,
            )
        )
        await self.session.execute(stmt)
        await self.session.commit()
