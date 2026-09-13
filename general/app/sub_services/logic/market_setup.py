from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.asset_history import AssetHistoryCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud


# Шаг и границы лота нужны только копиботу v3 с компаундингом: он торгует своим
# счётом, и когда на балансе перестаёт набираться minQty, биржа такой ордер не
# примет. Остальные боты парка считают на фиксированный баланс и в лот не
# упираются — для них эти ключи просто лежат рядом.
#
# Список общий с `StartTestBotsCommand.get_market_data`, который догружает пару
# на ходу: собранная там запись обязана выглядеть так же, иначе копибот v3
# упал бы на отсутствующем ключе именно на догруженной паре.
LOT_DATA_KEYS = ("step_size", "min_qty", "max_qty", "min_price", "max_price")


class MarketDataBuilder:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.asset_crud = AssetHistoryCrud(session)
        self.exchange_crud = AssetExchangeSpecCrud(session)

    async def build(self) -> dict:
        shared_data = {}
        symbols = await self.asset_crud.get_all_active_pairs()

        # Один запрос на все активные пары: их сотни, а с шардированием
        # запрос на каждую пару множился ещё и на число шардов.
        market_data_by_symbol = (
            await self.exchange_crud.get_market_data_by_symbols(symbols)
        )

        for symbol in symbols:
            market_data = market_data_by_symbol.get(symbol)

            if not market_data:
                shared_data[symbol] = {
                    "tick_size": None,
                    "maker_commission_rate": None,
                    "taker_commission_rate": None,
                    **{key: None for key in LOT_DATA_KEYS},
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
                # Через Decimal(str(...)), как и tick_size: значения уезжают в
                # арифметику количества, и двоичный хвост float там ни к чему.
                **{
                    key: (
                        Decimal(str(market_data[key]))
                        if market_data.get(key) is not None
                        else None
                    )
                    for key in LOT_DATA_KEYS
                },
            }

        return shared_data
