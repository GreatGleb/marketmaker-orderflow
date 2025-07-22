import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func, update
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestOrder, TestBot
from app.crud.test_bot import TestBotCrud

UTC = timezone.utc


async def update_bot_profits():
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with (dsm.get_session() as session):
        now = datetime.now(UTC)

        bot_crud = TestBotCrud(session)
        profits_data = await bot_crud.get_sorted_by_profit(just_copy_bots=True)#since=timedelta(hours=6), just_not_copy_bots=True, just_copy_bots=True

        earliest_query = select(
            func.min(TestOrder.created_at).label('earliest_date')
        )

        earliest_date = (await session.execute(earliest_query)).scalar()

        update_data = []
        bot_stats = []

        for bot_id, total_profit, total_orders, successful_orders in profits_data:
            update_data.append({'id': bot_id, 'total_profit': total_profit})

            success_percentage = (successful_orders / total_orders * 100) if total_orders > 0 else 0
            bot_stats.append({
                'bot_id': bot_id,
                'total_profit': total_profit,
                'total_orders': total_orders,
                'successful_orders': successful_orders,
                'success_percentage': success_percentage
            })

        # BATCH_SIZE = 1000
        # for i in range(0, len(update_data), BATCH_SIZE):
        #     batch = update_data[i:i + BATCH_SIZE]
        #     await session.execute(
        #         update(TestBot),
        #         batch
        #     )
        #     await session.commit()
        #     print(f"Обновлено записей: {i + len(batch)}/{len(update_data)}")

        # Сортируем по общей прибыли и выводим топ 10
        bot_stats.sort(key=lambda x: x['total_profit'], reverse=True)

        print(
            f"📊 Топ 10 прибыльных ботов\n"
            f"начиная от: {earliest_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"по состоянию на: {now.strftime('%Y-%m-%d %H:%M:%S')}:\n"
        )
        for idx, bot_data in enumerate(bot_stats[:100], 1):
            print(
                f"{idx}. Бот {bot_data['bot_id']} — 💰 Общая прибыль: {bot_data['total_profit']:.4f}, "
                f"📈 Успешных ордеров: {bot_data['successful_orders']}/{bot_data['total_orders']} ({bot_data['success_percentage']:.1f}%)"
            )

def main():
    print("⏳ Обновляем статистику ботов...\n")
    asyncio.run(update_bot_profits())


if __name__ == "__main__":
    main()
