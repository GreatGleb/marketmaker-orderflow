from sqlalchemy import select
from decimal import Decimal

from app.db.models import AssetExchangeSpec
from app.crud.base import BaseCrud
from app.crud.asset_history import AssetHistoryCrud


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

    async def get_symbols_characteristics_from_active_pairs(
        self
    ) -> dict:
        asset_crud = AssetHistoryCrud(self.session)
        active_symbols = await asset_crud.get_all_active_pairs()

        if not active_symbols:
            return {}

        stmt = (
            select(AssetExchangeSpec.symbol, AssetExchangeSpec.filters)
            .where(AssetExchangeSpec.symbol.in_(active_symbols))
        )
        result = await self.session.execute(stmt)
        all_exchange_specs = result.all()

        symbols_characteristics = {}
        for symbol, filters in all_exchange_specs:
            if not filters:
                symbols_characteristics[symbol] = {}
                continue

            filters_dict = self.transform_filters_list(filters)

            # print(filters_dict)
            # print('filters_dict')
            #
            # break

            symbols_characteristics[symbol] = filters_dict

        return symbols_characteristics

    def transform_filters_list(self, filters_list: list[dict]) -> dict:
        """
        Transforms a list of filter dictionaries into a categorized dictionary,
        converting numerical string values to Decimal where applicable.
        """
        transformed_data = {}

        filter_type_mapping = {
            'PRICE_FILTER': 'price'
        }

        for filter_item in filters_list:
            filter_type = filter_item.get('filterType')

            if not filter_type:
                continue

            dict_key = filter_type_mapping.get(filter_type, filter_type.lower())

            processed_filter_item = {}
            for key, value in filter_item.items():
                if isinstance(value, str):
                    try:
                        processed_filter_item[key] = Decimal(value)
                    except Exception:
                        processed_filter_item[key] = value
                else:
                    processed_filter_item[key] = value

            transformed_data[dict_key] = processed_filter_item

        return transformed_data
