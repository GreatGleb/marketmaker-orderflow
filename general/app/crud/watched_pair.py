from sqlalchemy import select

from app.db.models import AssetExchangeSpec, WatchedPair

from app.crud.base import BaseCrud


class WatchedPairCrud(BaseCrud[WatchedPair]):
    def __init__(self, session):
        super().__init__(session, WatchedPair)

    async def get_symbol_to_id_map(self) -> dict[str, int]:
        stmt = select(AssetExchangeSpec.symbol, AssetExchangeSpec.id).join(
            WatchedPair.asset_exchange
        )
        result = await self.session.execute(stmt)
        return {row[0]: row[1] for row in result.fetchall()}
