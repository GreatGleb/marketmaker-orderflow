from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.models import TestBot
from app.crud.base import BaseCrud


class TestBotCrud(BaseCrud[TestBot]):

    def __init__(self, session: AsyncSession):
        super().__init__(session, TestBot)

    async def get_active_bots(self):
        stmt = select(TestBot).where(TestBot.is_active.is_(True))
        result = await self.session.execute(stmt)
        return result.scalars().all()
