import asyncio

from datetime import datetime, timedelta, timezone

from fastapi import Depends

from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.asset_order_book import AssetOrderBookCrud
from app.dependencies import get_session, resolve_crud
from app.utils import Command, CommandResult


class OrderBookCleaner(Command):
    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        crud: AssetOrderBookCrud = resolve_crud(AssetOrderBookCrud),
    ) -> CommandResult:

        # Timestamp в миллисекундах, 24 часа назад
        cutoff = int(
            (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp()
            * 1000
        )
        await crud.delete_older_than(cutoff_timestamp=cutoff)

        return CommandResult(success=True)


async def main() -> None:
    await OrderBookCleaner().run_async()


if __name__ == "__main__":
    print("🧹 Starting OrderBookCleaner...")
    asyncio.run(main())
    print("✅ OrderBookCleaner finished.")
