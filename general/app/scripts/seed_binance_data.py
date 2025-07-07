import asyncio
import httpx
from datetime import datetime

from app.config import settings
from app.crud.asset_pair import AssetPairCrud
from app.crud.exchange_pair_spec import AssetExchangeSpecCrud
from app.db.base import DatabaseSessionManager


async def fetch_binance_data():
    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://fapi.binance.com/fapi/v1/exchangeInfo"
        )
        response.raise_for_status()
        return response.json()


async def seed_binance_data():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        asset_crud = AssetPairCrud(session)
        spec_crud = AssetExchangeSpecCrud(session)

        data = await fetch_binance_data()
        symbols = data.get("symbols", [])

        for s in symbols:
            base_asset = s["baseAsset"]
            quote_asset = s["quoteAsset"]
            pair = s["symbol"]

            asset = await asset_crud.get_or_create(
                pair, base_asset, quote_asset
            )

            spec_data = {
                "source": "BINANCE",
                "asset_pairs_id": asset.id,
                "contract_type": s["contractType"],
                "symbol": s["symbol"],
                "delivery_date": (
                    datetime.fromtimestamp(s["deliveryDate"] / 1000)
                    if s["deliveryDate"]
                    else None
                ),
                "onboard_date": (
                    datetime.fromtimestamp(s["onboardDate"] / 1000)
                    if s["onboardDate"]
                    else None
                ),
                "status": s["status"],
                "maint_margin_percent": s.get("maintMarginPercent"),
                "required_margin_percent": s.get("requiredMarginPercent"),
                "base_asset": base_asset,
                "quote_asset": quote_asset,
                "margin_asset": s.get("marginAsset"),
                "price_precision": s.get("pricePrecision"),
                "quantity_precision": s.get("quantityPrecision"),
                "base_asset_precision": s.get("baseAssetPrecision"),
                "quote_precision": s.get("quotePrecision"),
                "underlying_type": s.get("underlyingType"),
                "underlying_sub_type": s.get("underlyingSubType"),
                "settle_plan": s.get("settlePlan"),
                "trigger_protect": s.get("triggerProtect"),
                "filters": s.get("filters"),
                "order_type": s.get("orderType"),
                "time_in_force": s.get("timeInForce"),
                "liquidation_fee": s.get("liquidationFee"),
                "market_take_bound": s.get("marketTakeBound"),
            }

            await spec_crud.create(spec_data)

        await session.commit()
        print(f"✅ Seeded {len(symbols)} pairs from Binance.")


if __name__ == "__main__":
    asyncio.run(seed_binance_data())
