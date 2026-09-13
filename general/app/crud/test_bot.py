from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, case, distinct, update
from sqlalchemy.dialects.postgresql import insert
from datetime import datetime, timezone

from app.db.models import TestBot, TestOrder
from app.crud.base import BaseCrud

UTC = timezone.utc


# Колонки-маркеры: тип бота не хранится отдельным полем, он выводится из того,
# какая из них не NULL. Заводя следующую версию копибота, добавьте её сюда —
# `just_not_copy_bots` строится перебором этого списка, и забытая колонка
# означает, что новый копибот попадёт в пул доноров обычных ботов. Цепочка
# отбора замкнётся в кольцо (v3 → v2 → v1 → v3), а собственных торговых
# параметров у копибота нет: донор соберётся из нулей, и это не упадёт и никак
# не проявится в логах.
COPYBOT_MARKER_COLUMNS = (
    "copy_bot_min_time_profitability_min",
    "copybot_v2_time_in_minutes",
    "copybot_v3_time_in_minutes",
)


def active_bots_subquery(
    just_copy_bots=False,
    just_copy_bots_v2=False,
    just_copy_bots_v3=False,
    just_not_copy_bots=False,
    symbol=None,
):
    """id активных ботов нужного вида.

    Вынесено из `get_sorted_by_profit`, потому что тот же отбор нужен
    статистике по свёрткам (`TestOrderRollupCrud.profit_by_bot`). Два
    расходящихся определения «какие боты в отчёте» — это два отчёта,
    которые нельзя сравнивать между собой.
    """
    query = select(TestBot.id).where(TestBot.is_active == True)

    if just_copy_bots:
        query = query.where(
            TestBot.copy_bot_min_time_profitability_min.is_not(None)
        )
    elif just_copy_bots_v2:
        query = query.where(TestBot.copybot_v2_time_in_minutes.is_not(None))
    elif just_copy_bots_v3:
        query = query.where(TestBot.copybot_v3_time_in_minutes.is_not(None))
    elif just_not_copy_bots:
        query = query.where(*(
            getattr(TestBot, column).is_(None)
            for column in COPYBOT_MARKER_COLUMNS
        ))

    if symbol:
        query = query.where(TestBot.symbol == symbol)

    return query


class TestBotCrud(BaseCrud[TestBot]):

    def __init__(self, session: AsyncSession):
        super().__init__(session, TestBot)

    async def get_active_bots(self, shard: int = 0, shards: int = 1):
        """Активные боты; при `shards > 1` — только доля своего шарда.

        Симулятор запускается несколькими процессами, и парк делится по
        остатку от деления id. Остаток выбран потому, что не требует ни
        координации между процессами, ни лишнего запроса: каждый бот
        достаётся ровно одному шарду, пропусков и дублей нет при любом
        наборе id. Распределение по шардам при этом равномерное — id идут
        подряд, TRUNCATE в `new_bots.py` начинает нумерацию заново.
        """
        stmt = select(TestBot).where(TestBot.is_active.is_(True))

        if shards > 1:
            stmt = stmt.where(TestBot.id % shards == shard)

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        stmt = insert(TestBot).values(items)
        await self.session.execute(stmt)

    async def get_sorted_by_profit(
        self, since=None,
        just_copy_bots=False, just_copy_bots_v2=False,
        just_copy_bots_v3=False, just_not_copy_bots=False,
        symbol=None,
        by_referral_bot_id=False,
    ):
        active_bots = active_bots_subquery(
            just_copy_bots=just_copy_bots,
            just_copy_bots_v2=just_copy_bots_v2,
            just_copy_bots_v3=just_copy_bots_v3,
            just_not_copy_bots=just_not_copy_bots,
            symbol=symbol,
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

        if symbol:
            select_columns.append(func.array_agg(TestOrder.asset_symbol.distinct()).label("symbol"))

        if by_referral_bot_id:
            select_columns[0] = TestOrder.referral_bot_id

        profits_query = select(*select_columns).where(
            TestOrder.bot_id.in_(active_bots)
        )

        if by_referral_bot_id:
            profits_query = select(*select_columns).where(
                TestOrder.referral_bot_id.in_(active_bots)
            )

        if since is not None:
            now = datetime.now(UTC)
            time_ago = now - since

            profits_query = profits_query.where(
                TestOrder.created_at >= time_ago
            )

        if by_referral_bot_id:
            profits_query = profits_query.group_by(TestOrder.referral_bot_id)
        else:
            profits_query = profits_query.group_by(TestOrder.bot_id)

        profits_data = (await self.session.execute(profits_query)).all()

        return profits_data

    async def get_unique_copy_bot_min_time_profitability(self) -> list:
        stmt = select(
            distinct(TestBot.copy_bot_min_time_profitability_min)
        ).where(TestBot.copy_bot_min_time_profitability_min.is_not(None))
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_copybots(self):
        stmt = select(TestBot).where(
            TestBot.copy_bot_min_time_profitability_min.is_not(None)
        )
        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def get_copybots_v3(self, compound: bool = None):
        """Боты v3, опционально только компаундирующие.

        Нужен и симулятору (через него бот узнаёт свой стартовый баланс после
        перезапуска), и отчёту: тот показывает обе кривые рядом.
        """
        stmt = select(TestBot).where(
            TestBot.copybot_v3_time_in_minutes.is_not(None)
        )

        if compound is not None:
            stmt = stmt.where(
                TestBot.copybot_v3_compound_balance.is_(compound)
            )

        result = await self.session.execute(stmt)
        return result.scalars().all()

    async def set_balance(self, bot_id: int, balance) -> None:
        """Текущий баланс компаундирующего бота v3.

        Пишется после каждой сделки, потому что иначе кривая счёта не
        переживёт перезапуск симулятора. Прочитать своё состояние из
        `test_orders` бот не может: сделки уезжают в очередь Redis и
        вставляются пачками, то есть с задержкой.
        """
        await self.session.execute(
            update(TestBot).where(TestBot.id == bot_id).values(balance=balance)
        )
        await self.session.commit()

    async def mark_v3_stopped(self, bot_id: int, stopped_at=None) -> None:
        """Отметить, что на балансе перестал набираться минимальный лот.

        `is_active` намеренно остаётся `true`: `active_bots_subquery` отбирает
        только активных, и снятие флага убрало бы бота из всех отчётов — ровно
        там, где факт остановки важнее всего.
        """
        await self.session.execute(
            update(TestBot)
            .where(TestBot.id == bot_id)
            .values(copybot_v3_stopped_at=stopped_at or datetime.now(UTC))
        )
        await self.session.commit()

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