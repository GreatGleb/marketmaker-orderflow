from app.db.models import ExchangePairSpec

from app.crud.base import BaseCrud


class ExchangePairSpecCrud(BaseCrud[ExchangePairSpec]):
    def __init__(self, session):
        super().__init__(session, ExchangePairSpec)

    async def create(self, data: dict) -> ExchangePairSpec:
        spec = ExchangePairSpec(**data)
        self.session.add(spec)
        return spec
