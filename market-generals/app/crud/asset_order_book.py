from sqlalchemy.dialects.postgresql import insert
from sqlalchemy import delete, select
from sqlalchemy.orm import joinedload

from app.db.models import AssetOrderBook, AssetExchangeSpec
from app.crud.base import BaseCrud


class AssetOrderBookCrud(BaseCrud[AssetOrderBook]):
    def __init__(self, session):
        super().__init__(session, AssetOrderBook)

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        stmt = insert(AssetOrderBook).values(items)
        await self.session.execute(stmt)

    async def delete_older_than(self, cutoff_timestamp: int) -> None:
        stmt = delete(AssetOrderBook).where(
            AssetOrderBook.transaction_time < cutoff_timestamp
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_latest_by_symbol(self, symbol: str) -> AssetOrderBook | None:
        stmt = (
            select(AssetOrderBook)
            .join(AssetOrderBook.asset_exchange)
            .options(joinedload(AssetOrderBook.asset_exchange))
            .where(AssetExchangeSpec.symbol == symbol)
            .order_by(AssetOrderBook.transaction_time.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
