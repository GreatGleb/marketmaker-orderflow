from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from datetime import datetime

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
