from sqlalchemy.dialects.postgresql import insert

from app.db.models import AssetOrderBook
from app.crud.base import BaseCrud


class AssetOrderBookCrud(BaseCrud[AssetOrderBook]):
    def __init__(self, session):
        super().__init__(session, AssetOrderBook)

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        stmt = insert(AssetOrderBook).values(items)
        await self.session.execute(stmt)
