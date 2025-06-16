from app.db.models import AssetsHistory

from app.crud.base import BaseCrud


class AssetsHistoryCrud(BaseCrud[AssetsHistory]):
    def __init__(self, session):
        super().__init__(session, AssetsHistory)

    async def bulk_create(self, items: list[dict]) -> None:
        objects = [AssetsHistory(**item) for item in items]
        self.session.add_all(objects)
