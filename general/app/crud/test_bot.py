from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case
from sqlalchemy.dialects.postgresql import insert
from datetime import datetime, timedelta, timezone

from app.db.models import TestBot, TestOrder
from app.crud.base import BaseCrud

from app.db.base import DatabaseSessionManager
from app.config import settings

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

    async def get_sorted_by_profit(self, since = None, just_copy_bots = False, just_not_copy_bots = False):
        bots = await self.get_active_bots()

        profits_query = select(
            TestOrder.bot_id,
            func.coalesce(func.sum(TestOrder.profit_loss), None).label('total_profit'),
            func.count(TestOrder.id).label('total_orders'),
            func.sum(case((TestOrder.profit_loss > 0, 1), else_=0)).label('successful_orders')
        ).where(
            TestOrder.bot_id.in_([bot.id for bot in bots])
        )

        if since is not None:
            now = datetime.now(UTC)
            time_ago = now - since
            time_ago = time_ago.replace(tzinfo=None)

            profits_query = profits_query.where(TestOrder.created_at >= time_ago)

        if just_copy_bots:
            profits_query = profits_query.where(
                TestBot.copy_bot_min_time_profitability_min.is_not(None),
            )
        elif just_not_copy_bots:
            profits_query = profits_query.where(
                TestBot.copy_bot_min_time_profitability_min.is_(None),
            )

        profits_query = profits_query.group_by(TestOrder.bot_id)

        profits_data = (await self.session.execute(profits_query)).all()

        return profits_data

