from sqlalchemy import select

from app.db.models import AssetExchangeSpec
from app.crud.base import BaseCrud


class AssetExchangeSpecCrud(BaseCrud[AssetExchangeSpec]):
    def __init__(self, session):
        super().__init__(session, AssetExchangeSpec)

    async def create(self, data: dict) -> AssetExchangeSpec:
        spec = AssetExchangeSpec(**data)
        self.session.add(spec)
        return spec

    async def get_step_size_by_symbol(
        self, symbol: str
    ) -> dict[str, float] | None:
        stmt = (
            select(AssetExchangeSpec.filters)
            .where(AssetExchangeSpec.symbol == symbol)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        filters = result.scalar_one_or_none()

        if not filters:
            return None

        price_filter = next(
            (f for f in filters if f.get("filterType") == "PRICE_FILTER"), None
        )
        lot_size_filter = next(
            (f for f in filters if f.get("filterType") == "LOT_SIZE"), None
        )

        market_lot_filter = next(
            (f for f in filters if f.get("filterType") == "MARKET_LOT_SIZE"),
            None,
        )

        return {
            "tick_size": (
                float(price_filter["tickSize"]) if price_filter else None
            ),
            "step_size": (
                float(lot_size_filter["stepSize"]) if lot_size_filter else None
            ),
            "market_step_size": (
                float(market_lot_filter["stepSize"])
                if market_lot_filter
                else None
            ),
        }
