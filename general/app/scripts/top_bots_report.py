import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, func, update, case
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

        profits_query = select(
            TestOrder.bot_id,
            func.coalesce(func.sum(TestOrder.profit_loss), None).label('total_profit'),
            func.count(TestOrder.id).label('total_orders'),
            func.sum(case((TestOrder.profit_loss > 0, 1), else_=0)).label('successful_orders')
        ).where(
            TestOrder.bot_id.in_([bot.id for bot in bots])
        ).group_by(TestOrder.bot_id)

        profits_data = (await session.execute(profits_query)).all()

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

        # BATCH_SIZE = 100
        # for i in range(0, len(update_data), BATCH_SIZE):
        #     batch = update_data[i:i + BATCH_SIZE]
        #     await session.execute(
        #         update(TestBot),
        #         batch
        #     )
        #     await session.commit()
        #     # print(f"Обновлено записей: {i + len(batch)}/{len(update_data)}")

        # Сортируем по общей прибыли и выводим топ 10
        bot_stats.sort(key=lambda x: x['total_profit'], reverse=True)

        print(
            f"📊 Топ 10 прибыльных ботов\n"
            f"начиная от: {earliest_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"по состоянию на: {now.strftime('%Y-%m-%d %H:%M:%S')}:\n"
        )
        for idx, bot_data in enumerate(bot_stats[:10], 1):
            print(
                f"{idx}. Бот {bot_data['bot_id']} — 💰 Общая прибыль: {bot_data['total_profit']:.4f}, "
                f"📈 Успешных ордеров: {bot_data['successful_orders']}/{bot_data['total_orders']} ({bot_data['success_percentage']:.1f}%)"
            )

def main():
    print("⏳ Обновляем статистику ботов...\n")
    asyncio.run(update_bot_profits())


if __name__ == "__main__":
    main()
