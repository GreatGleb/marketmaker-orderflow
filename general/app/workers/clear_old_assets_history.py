import asyncio
from datetime import datetime, timedelta, UTC

from fastapi.params import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.asset_history import AssetHistoryCrud
from app.dependencies import get_session, resolve_crud
from app.utils import Command, CommandResult


class ClearOldAssetsHistoryCommand(Command):

    async def command(
        self,
        session: AsyncSession = Depends(get_session),
        asset_crud: AssetHistoryCrud = resolve_crud(AssetHistoryCrud),
    ) -> CommandResult:

        cutoff = datetime.now(UTC) - timedelta(hours=1)
        await asset_crud.delete_older_than(cutoff)
        return CommandResult(success=True)


async def main() -> None:
    await ClearOldAssetsHistoryCommand().run_async()


if __name__ == "__main__":
    print("🧹 Starting ClearOldAssetsHistoryCommand...")
    asyncio.run(main())
    print("✅ ClearOldAssetsHistoryCommand finished.")
