from decimal import Decimal

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import aliased
from sqlalchemy import select, func

from app.db.models import AssetHistory
from app.crud.base import BaseCrud


class AssetHistoryCrud(BaseCrud[AssetHistory]):
    def __init__(self, session):
        super().__init__(session, AssetHistory)

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
        volatility_cte = (
            select(
                AssetHistory.symbol,
                func.first_value(AssetHistory.last_price).over(
                    partition_by=AssetHistory.symbol,
                    order_by=AssetHistory.event_time.asc()
                ).label("old_price"),
                func.first_value(AssetHistory.last_price).over(
                    partition_by=AssetHistory.symbol,
                    order_by=AssetHistory.event_time.desc()
                ).label("new_price"),
            )
            .where(AssetHistory.event_time >= since)
        ).cte("volatility_calculations")

        final_query = (
            select(
                volatility_cte.c.symbol,
                volatility_cte.c.old_price,
                volatility_cte.c.new_price,
                func.abs(
                    (volatility_cte.c.new_price - volatility_cte.c.old_price) / volatility_cte.c.old_price
                ).label("volatility")
            )
            .order_by(
                func.abs(
                    (volatility_cte.c.new_price - volatility_cte.c.old_price) / volatility_cte.c.old_price
                ).desc()
            )
            .limit(1)
        )

        result = await self.session.execute(final_query)
        return result.first()

    async def get_latest_price(self, symbol: str) -> Decimal | None:
        stmt = (
            select(AssetHistory.last_price)
            .where(AssetHistory.symbol == symbol)
            .order_by(AssetHistory.event_time.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_active_pairs(self):
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

        result = await self.session.execute(new_prices)
        return result.scalars().all()
