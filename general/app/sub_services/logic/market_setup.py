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
            # Один запрос на пару: шаги и ставки лежат в одной строке
            # asset_exchange_specs.
            market_data = await self.exchange_crud.get_market_data_by_symbol(
                symbol
            )

            if not market_data:
                shared_data[symbol] = {
                    "tick_size": None,
                    "maker_commission_rate": None,
                    "taker_commission_rate": None,
                }
                continue

            tick_size = market_data["tick_size"]

            shared_data[symbol] = {
                "tick_size": (
                    Decimal(str(tick_size)) if tick_size is not None else None
                ),
                # None = ставка не засеяна, потребитель берёт константу.
                # Заполняется app/scripts/seed_commission_rates.py.
                "maker_commission_rate": market_data["maker_commission_rate"],
                "taker_commission_rate": market_data["taker_commission_rate"],
            }

        return shared_data
