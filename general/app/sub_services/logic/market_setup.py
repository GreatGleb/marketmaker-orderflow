from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud


class MarketDataBuilder:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.asset_crud = AssetHistoryCrud(session)
        self.exchange_crud = AssetExchangeSpecCrud(session)

    async def build(self) -> dict:
        shared_data = {}
        symbols = await self.asset_crud.get_all_active_pairs()

        for symbol in symbols:
            step_sizes = await self.exchange_crud.get_step_size_by_symbol(
                symbol
            )
            shared_data[symbol] = {
                "tick_size": (
                    Decimal(str(step_sizes.get("tick_size")))
                    if step_sizes
                    else None
                )
            }

        return shared_data
