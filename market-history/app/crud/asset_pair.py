from sqlalchemy import select

from app.crud.base import BaseCrud
from app.db.models import AssetPair


class AssetPairCrud(BaseCrud[AssetPair]):
    def __init__(self, session):
        super().__init__(session, AssetPair)

    async def get_or_create(
        self, pair: str, base_asset: str, quote_asset: str
    ) -> AssetPair:
        stmt = select(AssetPair).where(
            AssetPair.pair == pair,
            AssetPair.base_asset == base_asset,
            AssetPair.quote_asset == quote_asset,
        )
        result = await self.session.execute(stmt)
        asset_pair = result.scalar_one_or_none()

        if asset_pair:
            return asset_pair

        asset_pair = AssetPair(
            pair=pair, base_asset=base_asset, quote_asset=quote_asset
        )
        self.session.add(asset_pair)
        await self.session.flush()
        return asset_pair
