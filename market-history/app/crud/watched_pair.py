from sqlalchemy import select

from app.db.models import ExchangePairSpec, WatchedPair

from app.crud.base import BaseCrud


class WatchedPairCrud(BaseCrud[WatchedPair]):
    def __init__(self, session):
        super().__init__(session, WatchedPair)

    async def get_all_with_exchange_symbols(self) -> list[str]:
        stmt = select(ExchangePairSpec.symbol).join(WatchedPair.exchange_pair)
        result = await self.session.execute(stmt)
        return [row[0] for row in result.fetchall()]
