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
        ah_old = aliased(AssetHistory)
        ah_new = aliased(AssetHistory)

        sub_query_old = (
            select(
                ah_old.symbol, func.min(ah_old.event_time).label("min_time")
            )
            .where(ah_old.event_time >= since)
            .group_by(ah_old.symbol)
            .subquery()
        )

        sub_query_new = (
            select(
                ah_new.symbol, func.max(ah_new.event_time).label("max_time")
            )
            .where(ah_new.event_time >= since)
            .group_by(ah_new.symbol)
            .subquery()
        )

        old_prices = (
            select(AssetHistory.symbol, AssetHistory.last_price)
            .join(
                sub_query_old,
                (AssetHistory.symbol == sub_query_old.c.symbol)
                & (AssetHistory.event_time == sub_query_old.c.min_time),
            )
            .subquery()
        )

        new_prices = (
            select(AssetHistory.symbol, AssetHistory.last_price)
            .join(
                sub_query_new,
                (AssetHistory.symbol == sub_query_new.c.symbol)
                & (AssetHistory.event_time == sub_query_new.c.max_time),
            )
            .subquery()
        )

        volatility_query = (
            select(
                new_prices.c.symbol,
                new_prices.c.last_price,
                new_prices.c.last_price.label("new_price"),
                old_prices.c.last_price.label("old_price"),
                func.abs(
                    (new_prices.c.last_price - old_prices.c.last_price)
                    / old_prices.c.last_price
                ).label("volatility"),
            )
            .join(old_prices, old_prices.c.symbol == new_prices.c.symbol)
            .order_by(
                func.abs(
                    (new_prices.c.last_price - old_prices.c.last_price)
                    / old_prices.c.last_price
                ).desc()
            )
            .limit(1)
        )

        result = await self.session.execute(volatility_query)
        row = result.first()

        return row if row else None

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
