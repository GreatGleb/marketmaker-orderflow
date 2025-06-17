from app.db.models import AssetHistory

from app.crud.base import BaseCrud


class AssetHistoryCrud(BaseCrud[AssetHistory]):
    def __init__(self, session):
        super().__init__(session, AssetHistory)

    async def bulk_create(self, items: list[dict]) -> None:
        objects = [AssetHistory(**item) for item in items]
        self.session.add_all(objects)
