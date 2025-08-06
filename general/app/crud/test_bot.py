from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, distinct, update
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
        self, since=None, just_copy_bots=False, just_copy_bots_v2=False, just_not_copy_bots=False, add_asset_symbol=False, symbol=None
    ):
        active_bots_subquery = select(TestBot.id).where(
            TestBot.is_active == True
        )

        if just_copy_bots:
            active_bots_subquery = active_bots_subquery.where(
                TestBot.copy_bot_min_time_profitability_min.is_not(None)
            )
        elif just_copy_bots_v2:
            active_bots_subquery = active_bots_subquery.where(
                TestBot.copybot_v2_time_in_minutes.is_not(None)
            )
        elif just_not_copy_bots:
            active_bots_subquery = active_bots_subquery.where(
                TestBot.copy_bot_min_time_profitability_min.is_(None),
                TestBot.copybot_v2_time_in_minutes.is_(None)
            )

        if symbol:
            active_bots_subquery = active_bots_subquery.where(
                TestBot.symbol == symbol
            )

        select_columns = [
            TestOrder.bot_id,
            func.coalesce(func.sum(TestOrder.profit_loss), None).label(
                "total_profit"
            ),
            func.count(TestOrder.id).label("total_orders"),
            func.sum(case((TestOrder.profit_loss > 0, 1), else_=0)).label(
                "successful_orders"
            ),
        ]

        if add_asset_symbol:
            select_columns.append(func.array_agg(TestOrder.asset_symbol.distinct()).label("asset_symbol"))

        profits_query = select(*select_columns).where(TestOrder.bot_id.in_(active_bots_subquery))

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

    async def get_bot_symbols(self):
        stmt = select(distinct(TestBot.symbol)).where(
            TestBot.is_active.is_(True)
        )
        result = await self.session.execute(stmt)
        result = result.scalars().all()
        result = [elem for elem in result if elem]
        return result

    async def deactivate_bot(self, symbol):
        result = await self.session.execute(
            select(TestBot.id)
            .where(
                TestBot.symbol == symbol,
                TestBot.is_active == True
            )
        )

        bot_ids = [row[0] for row in result]

        if not bot_ids:
            print(f"Нет активных ботов для символа '{symbol}'.")
            return

        BATCH_SIZE = 100

        batches = [
            bot_ids[i:i + BATCH_SIZE]
            for i in range(0, len(bot_ids), BATCH_SIZE)
        ]

        for batch_of_ids in batches:
            await self.session.execute(
                update(TestBot)
                .where(TestBot.id.in_(batch_of_ids))
                .values(is_active=False)
            )
            await self.session.commit()
            print("батч")
