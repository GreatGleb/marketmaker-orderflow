from decimal import Decimal
from app.db.base import DatabaseSessionManager
from app.crud.test_bot import TestBotCrud
from app.config import settings
import asyncio


async def create_bots(symbol="BTCUSDT"):
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        bot_crud = TestBotCrud(session)

        # start_ticks_values = [1, 15, 20, 30, 40, 60, 80, 100, 120, 146, 184, 222, 260, 298, 336, 374, 412, 450, 500]  # Y axis
        # stop_lose_ticks_values = [1, 10, 20, 40, 60, 80, 90, 100, 126, 142, 158, 174, 190, 206, 222, 238, 254]  # X axis
        # stop_win_ticks_values = [1, 5, 10, 20, 30, 40]

        start_ticks_values = [10, 250, 500, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000, 20000, 30000]  # Y axis
        stop_lose_ticks_values = [1, 100, 200, 300, 500, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000, 20000, 50000]  # X axis
        stop_win_ticks_values = [1, 5, 20, 40, 60, 70, 160, 300, 600, 1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000, 9000, 10000]

        try:
            for start in start_ticks_values:
                for stop_lose in stop_lose_ticks_values:
                    new_bots = []
                    for stop_win in stop_win_ticks_values:
                        bot_data = {
                            "symbol": symbol,
                            "balance": Decimal("1000.0"),
                            "stop_success_ticks": stop_win,
                            "stop_loss_ticks": stop_lose,
                            "start_updown_ticks": start,
                            "is_active": True,
                        }
                        new_bots.append(bot_data)

                    await bot_crud.bulk_create(new_bots)
                    await session.commit()
            print("✅ Успешно создано ботов.")
        except Exception as e:
            print(e)


if __name__ == "__main__":
    asyncio.run(create_bots())
