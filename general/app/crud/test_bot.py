from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, distinct
from sqlalchemy.dialects.postgresql import insert
from datetime import datetime, timezone

from app.db.models import TestBot, TestOrder
from app.crud.base import BaseCrud

UTC = timezone.utc


class TestBotCrud(BaseCrud[TestBot]):

    def __init__(self, session: AsyncSession):
        super().__init__(session, TestBot)

    async def get_active_bots(self):
        stmt = select(TestBot).where(TestBot.is_active.is_(True))
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        stmt = insert(TestBot).values(items)
        await self.session.execute(stmt)

    async def get_sorted_by_profit(
        self, since=None, just_copy_bots=False, just_not_copy_bots=False
    ):
        active_bots_subquery = select(TestBot.id).where(
            TestBot.is_active == True
        )

        if just_copy_bots:
            active_bots_subquery = active_bots_subquery.where(
                TestBot.copy_bot_min_time_profitability_min.is_not(None)
            )
        elif just_not_copy_bots:
            active_bots_subquery = active_bots_subquery.where(
                TestBot.copy_bot_min_time_profitability_min.is_(None)
            )

        profits_query = select(
            TestOrder.bot_id,
            func.coalesce(func.sum(TestOrder.profit_loss), None).label(
                "total_profit"
            ),
            func.count(TestOrder.id).label("total_orders"),
            func.sum(case((TestOrder.profit_loss > 0, 1), else_=0)).label(
                "successful_orders"
            ),
        ).where(TestOrder.bot_id.in_(active_bots_subquery))

        if since is not None:
            now = datetime.now(UTC)
            time_ago = now - since

            profits_query = profits_query.where(
                TestOrder.created_at >= time_ago
            )

        profits_query = profits_query.group_by(TestOrder.bot_id)
        profits_data = (await self.session.execute(profits_query)).all()

        return profits_data

    async def get_unique_copy_bot_min_time_profitability(self) -> list:
        stmt = select(
            distinct(TestBot.copy_bot_min_time_profitability_min)
        ).where(TestBot.copy_bot_min_time_profitability_min.is_not(None))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_bots_with_profitability_time(self):
        stmt = select(TestBot).where(
            TestBot.copy_bot_min_time_profitability_min.is_not(None)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_with_volatility_by_id(self, bot_id: int):
        stmt = select(TestBot).where(
            TestBot.id == bot_id,
            TestBot.min_timeframe_asset_volatility.is_not(None),
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_bot_by_id(self, bot_id: int):
        stmt = select(TestBot).where(
            TestBot.id == bot_id,
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_unique_min_timeframe_volatility_values(self):
        stmt = select(distinct(TestBot.min_timeframe_asset_volatility)).where(
            TestBot.min_timeframe_asset_volatility.is_not(None)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()
