import asyncio
from sqlalchemy import select, or_, func, exists

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import AssetPair, AssetExchangeSpec, WatchedPair


async def seed_usdt_watched_pairs():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        # Шаг 1: Найти пары, где base_asset или quote_asset равен "usdt"
        asset_pair_stmt = select(AssetPair.id).where(
            func.lower(AssetPair.base_asset) == "bio",
            func.lower(AssetPair.quote_asset) == "usdt"
        )
        asset_pair_ids_result = await session.execute(asset_pair_stmt)
        asset_pair_ids_result = asset_pair_ids_result.fetchall()
        # asset_pair_ids_result = [asset_pair_ids_result[0]]
        asset_pair_ids = [row[0] for row in asset_pair_ids_result]

        if not asset_pair_ids:
            print("🚫 No pairs with USDT in asset_pairs")
            return

        # Шаг 2: Найти exchange_pair_specs, ссылающиеся на эти asset_pairs
        asset_exchange_stmt = select(AssetExchangeSpec.id).where(
            AssetExchangeSpec.asset_pairs_id.in_(asset_pair_ids)
        )
        asset_exchange_result = await session.execute(asset_exchange_stmt)
        asset_exchange_ids = [
            row[0] for row in asset_exchange_result.fetchall()
        ]

        if not asset_exchange_ids:
            print("🚫 No asset_exchange_specs for asset_pairs found")
            return

        # Шаг 3: Добавить в watched_pairs (если ещё не добавлены)
        added_count = 0
        for ex_id in asset_exchange_ids:
            exists_stmt = select(
                exists().where(WatchedPair.asset_exchange_id == ex_id)
            )
            result = await session.execute(exists_stmt)
            already_exists = result.scalar()

            if not already_exists:
                session.add(WatchedPair(asset_exchange_id=ex_id))
                added_count += 1

        await session.commit()
        print(f"✅ Added {added_count} entries to watched_pairs")


if __name__ == "__main__":
    asyncio.run(seed_usdt_watched_pairs())
