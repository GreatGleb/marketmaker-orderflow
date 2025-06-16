import asyncio
from sqlalchemy import select, or_, func, exists

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import AssetPair, ExchangePairSpec, WatchedPair


async def seed_usdt_watched_pairs():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        # Шаг 1: Найти пары, где base_asset или quote_asset равен "usdt"
        asset_pair_stmt = select(AssetPair.id).where(
            or_(
                func.lower(AssetPair.base_asset) == "usdt",
                func.lower(AssetPair.quote_asset) == "usdt",
            )
        )
        asset_pair_ids_result = await session.execute(asset_pair_stmt)
        asset_pair_ids = [row[0] for row in asset_pair_ids_result.fetchall()]

        if not asset_pair_ids:
            print("🚫 No pairs with USDT in asset_pairs")
            return

        # Шаг 2: Найти exchange_pair_specs, ссылающиеся на эти asset_pairs
        exchange_pair_stmt = select(ExchangePairSpec.id).where(
            ExchangePairSpec.asset_pairs_id.in_(asset_pair_ids)
        )
        exchange_pairs_result = await session.execute(exchange_pair_stmt)
        exchange_pair_ids = [
            row[0] for row in exchange_pairs_result.fetchall()
        ]

        if not exchange_pair_ids:
            print("🚫 No exchange_pair_specs for asset_pairs found")
            return

        # Шаг 3: Добавить в watched_pairs (если ещё не добавлены)
        added_count = 0
        for ex_id in exchange_pair_ids:
            exists_stmt = select(
                exists().where(WatchedPair.exchange_pair_id == ex_id)
            )
            result = await session.execute(exists_stmt)
            already_exists = result.scalar()

            if not already_exists:
                session.add(WatchedPair(exchange_pair_id=ex_id))
                added_count += 1

        await session.commit()
        print(f"✅ Added {added_count} entries to watched_pairs")


if __name__ == "__main__":
    asyncio.run(seed_usdt_watched_pairs())
