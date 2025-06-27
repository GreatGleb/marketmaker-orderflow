import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, func, update
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestOrder, TestBot

UTC = timezone.utc


async def update_bot_profits():

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        # Получаем всех ботов
        bots_result = await session.execute(select(TestBot))
        bots = bots_result.scalars().all()

        now = datetime.now(UTC)

        updates = []

        for bot in bots:
            # Общая прибыль
            total_stmt = select(
                func.coalesce(func.sum(TestOrder.profit_loss), 0)
            ).where(TestOrder.bot_id == bot.id)

            total_profit = (await session.execute(total_stmt)).scalar_one()

            # Обновляем данные в объекте
            await session.execute(
                update(TestBot)
                .where(TestBot.id == bot.id)
                .values(
                    total_profit=total_profit,
                )
            )

            updates.append((bot.id, total_profit))

        await session.commit()

        # Сортируем по общей прибыли и выводим топ 10
        updates.sort(key=lambda x: x[1], reverse=True)

        print(
            f"📊 Топ 10 прибыльных ботов по состоянию "
            f"на {now.strftime('%Y-%m-%d %H:%M:%S')}:\n"
        )
        for idx, (bot_id, total_profit) in enumerate(updates[:10], 1):
            print(f"{idx}. Бот {bot_id} — Общая прибыль: {total_profit:.4f}")


def main():
    print("⏳ Обновляем статистику ботов...\n")
    asyncio.run(update_bot_profits())


if __name__ == "__main__":
    main()
