import logging
from decimal import Decimal

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import aliased
from sqlalchemy import select, func

from app.db.models import AssetHistory
from app.crud.base import BaseCrud


class AssetHistoryCrud(BaseCrud[AssetHistory]):
    def __init__(self, session):
        super().__init__(session, AssetHistory)
        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        logging.info('AssetHistoryCrud init')

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        stmt = insert(AssetHistory).values(items)
        await self.session.execute(stmt)

    async def delete_older_than(self, cutoff_timestamp: datetime):
        stmt = delete(AssetHistory).where(
            AssetHistory.event_time < cutoff_timestamp
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_most_volatile_since(self, since: datetime):
        query = (
            select(
                AssetHistory.symbol,
                func.abs(
                    (func.max(AssetHistory.last_price) - func.min(AssetHistory.last_price))
                    / func.min(AssetHistory.last_price)
                ).label("volatility")
            )
            .where(AssetHistory.event_time >= since)
            .group_by(AssetHistory.symbol)
            .order_by(text("volatility DESC"))
            .limit(1)
        )

        result = await self.session.execute(query)
        return result.first()

    async def get_most_volatiles_since(self, since: datetime):
        query = (
            select(
                AssetHistory.symbol,
                func.abs(
                    (func.max(AssetHistory.last_price) - func.min(AssetHistory.last_price))
                    / func.min(AssetHistory.last_price)
                ).label("volatility")
            )
            .where(AssetHistory.event_time >= since)
            .group_by(AssetHistory.symbol)
            .order_by(text("volatility DESC"))
            .limit(10)
        )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_most_volatiles_since_from_symbols_list(self, since: datetime, symbols_list):
        query = (
            select(
                AssetHistory.symbol,
                func.abs(
                    (func.max(AssetHistory.last_price) - func.min(AssetHistory.last_price))
                    / func.min(AssetHistory.last_price)
                ).label("volatility")
            )
            .where(AssetHistory.event_time >= since)
            .where(AssetHistory.symbol.in_(symbols_list))
            .group_by(AssetHistory.symbol)
            .order_by(text("volatility DESC"))
            .limit(10)
        )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_latest_price(self, symbol: str) -> Decimal | None:
        stmt = (
            select(AssetHistory.last_price)
            .where(AssetHistory.symbol == symbol)
            .order_by(AssetHistory.event_time.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_active_pairs(self, is_need_full_info = False):
        result = []

        try:
            UTC = timezone.utc
            now = datetime.now(UTC)
            five_minutes_ago = now - timedelta(minutes=5)

            since = five_minutes_ago
            ah_new = aliased(AssetHistory)

            sub_query_new = (
                select(
                    ah_new.symbol, func.max(ah_new.event_time).label("max_time")
                )
                .where(ah_new.event_time >= since)
                .group_by(ah_new.symbol)
                .subquery()
            )

            new_prices = (
                select(AssetHistory.symbol, AssetHistory.last_price)
                .join(
                    sub_query_new,
                    (AssetHistory.symbol == sub_query_new.c.symbol)
                    & (AssetHistory.event_time == sub_query_new.c.max_time),
                )
            )

            if is_need_full_info:
                new_prices = (
                    select(AssetHistory)
                    .join(
                        sub_query_new,
                        (AssetHistory.symbol == sub_query_new.c.symbol)
                        & (AssetHistory.event_time == sub_query_new.c.max_time),
                    )
                )

            logging.info(f'getting new prices')

            result = await self.session.execute(new_prices)
            logging.info(f'result: {result}')
            result = result.scalars().all()
            logging.info(f'result: {result}')
        except Exception as e:
            logging.info(e)

        logging.info(f'resultt')

        return result
