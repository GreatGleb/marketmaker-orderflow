from sqlalchemy import select, distinct, update
from decimal import Decimal, DecimalException
import json

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

    async def get_or_create(self, spec_data: dict) -> tuple[AssetExchangeSpec, bool]:
        stmt = select(AssetExchangeSpec).where(
            AssetExchangeSpec.asset_pairs_id == spec_data["asset_pairs_id"]
        )
        result = await self.session.execute(stmt)
        spec = result.scalars().first()
        # spec = result.scalar_one_or_none()

        if spec:
            # Обновляем биржевые ограничения; комиссии в spec_data не входят.
            for key, value in spec_data.items():
                setattr(spec, key, value)
            return spec, False

        spec = AssetExchangeSpec(**spec_data)
        self.session.add(spec)
        return spec, True

    # Границы лота и цены нужны копиботу v3 с компаундингом: он торгует не
    # условной тысячей, а своим счётом, и когда на балансе перестаёт набираться
    # minQty, реальный ордер биржа не примет. Остальным ботам парка они не
    # нужны — те считают на фиксированный баланс и в лот не упираются.
    EMPTY_MARKET_DATA = {
        "tick_size": None,
        "step_size": None,
        "market_step_size": None,
        "market_min_qty": None,
        "market_max_qty": None,
        "min_notional": None,
        "min_qty": None,
        "max_qty": None,
        "min_price": None,
        "max_price": None,
    }

    @staticmethod
    def extract_step_sizes(filters) -> dict[str, Decimal | None]:
        """Шаги и границы цены и лота из JSON-фильтров Binance."""
        if not isinstance(filters, list) or not all(isinstance(f, dict) for f in filters):
            return dict(AssetExchangeSpecCrud.EMPTY_MARKET_DATA)

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

        def _from(source, key):
            try:
                value = Decimal(str(source[key]))
                return value if value.is_finite() and value >= 0 else None
            except (KeyError, TypeError, ValueError, DecimalException):
                return None

        notional_filter = next(
            (f for f in filters if f.get("filterType") == "MIN_NOTIONAL"), None
        )

        return {
            "tick_size": _from(price_filter, "tickSize"),
            "step_size": _from(lot_size_filter, "stepSize"),
            "market_step_size": _from(market_lot_filter, "stepSize"),
            "market_min_qty": _from(market_lot_filter, "minQty"),
            "market_max_qty": _from(market_lot_filter, "maxQty"),
            "min_notional": _from(notional_filter, "notional"),
            "min_qty": _from(lot_size_filter, "minQty"),
            "max_qty": _from(lot_size_filter, "maxQty"),
            "min_price": _from(price_filter, "minPrice"),
            "max_price": _from(price_filter, "maxPrice"),
        }

    async def get_step_size_by_symbol(
        self, symbol: str
    ) -> dict[str, Decimal | None] | None:
        stmt = (
            select(AssetExchangeSpec.filters)
            .where(AssetExchangeSpec.symbol == symbol)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        filters = result.scalar_one_or_none()

        if not filters:
            return None

        return self.extract_step_sizes(filters)

    async def get_market_data_by_symbol(self, symbol: str) -> dict | None:
        """Всё, что нужно симулятору по паре, одним запросом: шаги цены и лота
        плюс ставки комиссии — они лежат в одной строке asset_exchange_specs.

        None в ставках означает «не заполнено» (см.
        app/scripts/seed_commission_rates.py) — вызывающий код должен
        откатиться на константу из app/constants/commissions.py.
        """
        stmt = (
            select(
                AssetExchangeSpec.filters,
                AssetExchangeSpec.maker_commission_rate,
                AssetExchangeSpec.taker_commission_rate,
            )
            .where(AssetExchangeSpec.symbol == symbol)
            .limit(1)
        )
        row = (await self.session.execute(stmt)).first()

        if not row:
            return None

        filters, maker, taker = row

        return self._build_market_data(filters, maker, taker)

    async def get_market_data_by_symbols(
        self, symbols: list[str]
    ) -> dict[str, dict]:
        """То же, что get_market_data_by_symbol, но сразу по списку пар:
        один select ... where symbol in (...) вместо запроса на каждую.
        Активных пар на старте сотни, а с шардированием запросы ещё и
        множатся на число шардов.

        В ответе только те пары, что нашлись в asset_exchange_specs:
        отсутствие ключа означает «спеки не засеяны», вызывающий код сам
        решает, чем это заполнить.
        """
        if not symbols:
            return {}

        stmt = select(
            AssetExchangeSpec.symbol,
            AssetExchangeSpec.filters,
            AssetExchangeSpec.maker_commission_rate,
            AssetExchangeSpec.taker_commission_rate,
        ).where(AssetExchangeSpec.symbol.in_(set(symbols)))
        rows = (await self.session.execute(stmt)).all()

        market_data_by_symbol: dict[str, dict] = {}
        for symbol, filters, maker, taker in rows:
            # На пару может быть несколько строк (разные contract_type) —
            # берём первую, как это делал limit(1) в запросе на одну пару.
            if symbol in market_data_by_symbol:
                continue

            market_data_by_symbol[symbol] = self._build_market_data(
                filters, maker, taker
            )

        return market_data_by_symbol

    def _build_market_data(self, filters, maker, taker) -> dict:
        """Строка asset_exchange_specs -> шаги цены и лота плюс ставки."""
        market_data = self.extract_step_sizes(filters)
        market_data["maker_commission_rate"] = (
            Decimal(str(maker)) if maker is not None else None
        )
        market_data["taker_commission_rate"] = (
            Decimal(str(taker)) if taker is not None else None
        )

        return market_data

    async def set_commission_rates(
        self, symbol: str, maker_rate, taker_rate
    ) -> None:
        stmt = (
            update(AssetExchangeSpec)
            .where(AssetExchangeSpec.symbol == symbol)
            .values(
                maker_commission_rate=maker_rate,
                taker_commission_rate=taker_rate,
            )
        )
        await self.session.execute(stmt)

    async def get_symbols_without_commission_rates(self) -> list[str]:
        stmt = select(AssetExchangeSpec.symbol).where(
            AssetExchangeSpec.taker_commission_rate.is_(None)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

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

    async def get_all_symbols(self):
        stmt = select(AssetExchangeSpec.symbol)
        result = await self.session.execute(stmt)
        all_exchange_specs = result.all()

        return all_exchange_specs

    async def get_all_symbols_with_id_map(self):
        stmt = select(AssetExchangeSpec.symbol, AssetExchangeSpec.id)
        result = await self.session.execute(stmt)
        all_exchange_specs = result.all()

        return {row[0]: row[1] for row in all_exchange_specs}

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

    async def set_isolate_mode_and_leverage(self, binance_bot):
        stmt = select(distinct(AssetExchangeSpec.symbol)).where(AssetExchangeSpec.source == 'BINANCE')
        result = await self.session.execute(stmt)
        all_symbols = result.scalars().all()

        # position_info = await binance_bot._safe_from_time_err_call_binance(binance_bot.binance_client.futures_account)
        # file_name = "position_info.json"
        # with open(file_name, 'w', encoding='utf-8') as f:
        #     json.dump(position_info, f, ensure_ascii=False, indent=4)

        i = 0
        for symbol in all_symbols:
            print(f'{i} from {len(all_symbols)}')

            try:
                await binance_bot._safe_from_time_err_call_binance(
                    binance_bot.binance_client.futures_change_margin_type,
                    symbol=symbol, marginType='ISOLATED'
                )
            except Exception as e:
                print(f'Error when set isolated mode: {e}')
                pass

            try:
                await binance_bot._safe_from_time_err_call_binance(
                    binance_bot.binance_client.futures_change_leverage,
                    symbol=symbol, leverage=1
                )
            except Exception as e:
                print(f'Error when leverage: {e}')
                pass

            i = i + 1

        print('Finished setting isolate mode')

        return
