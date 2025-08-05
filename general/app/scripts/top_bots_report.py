import argparse
import asyncio
from datetime import datetime, timezone, timedelta

from sqlalchemy import select, func, update
from app.db.base import DatabaseSessionManager
from app.config import settings
from app.db.models import TestOrder, TestBot
from app.crud.test_bot import TestBotCrud

UTC = timezone.utc


async def update_bot_profits(hours: int = None, minutes: int = None, just_copy_bots: str = None, just_copy_bots_v2: str = None, just_not_copy_bots: str = None, top_count: int = 10):
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with (dsm.get_session() as session):
        now = datetime.now(UTC)

        bot_crud = TestBotCrud(session)

        since_timedelta = None
        if hours is not None or minutes is not None:
            since_timedelta = timedelta(hours=hours if hours is not None else 0,
                                        minutes=minutes if minutes is not None else 0)
            if since_timedelta == timedelta(0):
                since_timedelta = None

        profits_data = await bot_crud.get_sorted_by_profit(
            since=since_timedelta,
            just_copy_bots=just_copy_bots,
            just_copy_bots_v2=just_copy_bots_v2,
            just_not_copy_bots=just_not_copy_bots,
            add_asset_symbol=True,
            symbol='VICUSDT'
        )

        earliest_query = select(
            func.min(TestOrder.created_at).label('earliest_date')
        )

        earliest_date = (await session.execute(earliest_query)).scalar()

        if not earliest_date:
            print('Нет заказов у ботов')
            return

        update_data = []
        bot_stats = []

        for bot_id, total_profit, total_orders, successful_orders, symbol in profits_data:
            update_data.append({'id': bot_id, 'total_profit': total_profit})

            success_percentage = (successful_orders / total_orders * 100) if total_orders > 0 else 0
            bot_stats.append({
                'bot_id': bot_id,
                'symbol': symbol[0],
                'total_profit': total_profit,
                'total_orders': total_orders,
                'successful_orders': successful_orders,
                'success_percentage': success_percentage
            })

        if False:
            BATCH_SIZE = 100
            for i in range(0, len(update_data), BATCH_SIZE):
                batch = update_data[i:i + BATCH_SIZE]
                await session.execute(
                    update(TestBot),
                    batch
                )
                await session.commit()
                print(f"Обновлено записей: {i + len(batch)}/{len(update_data)}")

        bot_stats.sort(key=lambda x: x['total_profit'], reverse=True)

        if not top_count:
            top_count = 10

        print(
            f"📊 Топ {top_count} прибыльных ботов\n"
            f"начиная от: {earliest_date.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"по состоянию на: {now.strftime('%Y-%m-%d %H:%M:%S')}:\n"
        )
        for idx, bot_data in enumerate(bot_stats[:top_count], 1):
            print(
                f"{idx}. Бот {bot_data['bot_id']} — 💰 Общая прибыль: {bot_data['total_profit']:.4f}, "
                f"📈 Успешных ордеров: {bot_data['successful_orders']}/{bot_data['total_orders']} ({bot_data['success_percentage']:.1f}%)"
            )

def main():
    parser = argparse.ArgumentParser(description="Обновляет и выводит статистику прибыльности торговых ботов.")
    parser.add_argument('-H', '--hours', type=int,
                        help="Количество часов для фильтрации данных (например, 24).")
    parser.add_argument('-m', '--minutes', type=int,
                        help="Количество минут для фильтрации данных (например, 30).")
    parser.add_argument('-just_copy', '--just_copy_bots', type=str,
                        help="Только копиботы")
    parser.add_argument('-just_copy_v2', '--just_copy_bots_v2', type=str,
                        help="Только копиботы v2")
    parser.add_argument('-just_not_copy', '--just_not_copy_bots', type=str,
                        help="Только обычные боты")
    parser.add_argument('-top_count', '--top_count', type=int,
                        help="Количество ботов")

    args = parser.parse_args()

    print("⏳ Обновляем статистику ботов...\n")
    asyncio.run(update_bot_profits(hours=args.hours, minutes=args.minutes, just_copy_bots=args.just_copy_bots, just_copy_bots_v2=args.just_copy_bots_v2, just_not_copy_bots=args.just_not_copy_bots, top_count=args.top_count))


if __name__ == "__main__":
    main()
