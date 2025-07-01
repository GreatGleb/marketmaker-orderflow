from decimal import Decimal
from app.db.base import DatabaseSessionManager
from app.crud.test_bot import TestBotCrud
from app.config import settings
import asyncio


async def create_bots(symbol="BTCUSDT"):
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)

        entry_ticks_values = [1, 15, 20, 30, 40, 60, 80, 100, 120]  # Y axis
        exit_ticks_values = [1, 10, 20, 40, 60, 80, 90, 100]  # X axis

        new_bots = []
        for entry in entry_ticks_values:
            for exit_val in exit_ticks_values:
                bot_data = {
                    "symbol": symbol,
                    "balance": Decimal("1000.0"),
                    "stop_success_ticks": exit_val,
                    "stop_loss_ticks": entry,
                    "is_active": True,
                }
                new_bots.append(bot_data)

        await bot_crud.bulk_create(new_bots)
        await session.commit()
        print("✅ Успешно создано ботов.")


if __name__ == "__main__":
    asyncio.run(create_bots())
