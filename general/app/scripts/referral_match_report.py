"""Совпадал ли донор копибота с тем, кого выбрала бы функция отбора.

Копибот v1 не считает донора сам: воркер `set_profitable_bot` раз в 30 секунд
перебирает окна прибыльности и кладёт готовый конфиг в `copy_bot_{id}`, а бот
на открытии сделки читает готовое. Плата за это описана в
`.ai/docs/test-bots/08-gotchas.md`, пункт 10b:

    конфиг донора может отставать до 30 секунд, и если у донора временно нет
    прибыльного бота, воркер не перезаписывает ключ — копибот продолжит
    торговать последним известным конфигом.

Насколько это отставание портит отбор, до сих пор никто не мерил. Отчёт
меряет: по каждой сделке копибота смотрит, какое место занимал взятый донор
в рейтинге на тот момент.

Раньше на этот же вопрос отвечала колонка
`test_orders.referral_bot_from_profit_func` — бот пересчитывал донора прямо
на открытии и записывал рядом. Её выключили через четыре дня после появления, и по делу:
пересчёт делал на каждое открытие сделки те же тяжёлые агрегации, которые
воркер считает раз в цикл сразу для всех. Здесь то же самое считается
постфактум по свёрткам и в рантайме не стоит ничего.

Что показывают места:

    1        донор совпал с лучшим — попали точно;
    2-3      почти попали, обычная цена тридцатисекундного отставания;
    4+       рейтинг успел перетасоваться сильно;
    «не в отборе»  взятый донор в тот момент вообще не проходил условия —
                   был убыточен за своё окно. Вот это и есть настоящая
                   пропажа, а не отставание;
    «донора не было»  подходящих ботов не было ни одного, и воркер по
                      устройству оставил в Redis прежний ключ. Не ошибка
                      копибота, но сделки в это время идут по старому конфигу.

Копиботы v2 отчёт не разбирает. У них другой отбор: донор берётся среди
копиботов v1, а не среди обычных ботов (`get_copybot_config`), и мерить их
той же меркой нельзя — вышло бы сравнение с чужим рейтингом.

Окно по умолчанию — с рождения нынешнего парка. Раньше смотреть нельзя:
`new_bots.py` делает `TRUNCATE test_bots RESTART IDENTITY CASCADE`, номера
выдаются заново, и `bot_id` в старых свёртках указывает уже на других ботов
(сами свёртки при этом переживают чистку — внешнего ключа на `test_bots`
у них нет).

    python -m app.scripts.referral_match_report
    python -m app.scripts.referral_match_report -d 3      # за трое суток
    python -m app.scripts.referral_match_report --details # с разбивкой по ботам
"""
import argparse
import asyncio
import logging

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.crud.test_order_rollup import TestOrderRollupCrud
from app.db.base import DatabaseSessionManager
from app.db.models import TestBot

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

UTC = timezone.utc

# Границы разрядов для распределения мест. Первый разряд отдельный: только
# он означает «попали точно», всё остальное — уже расхождение той или иной
# величины.
RANK_BUCKETS = ((1, 1, "место 1 — точное попадание"),
                (2, 3, "места 2-3 — почти попали"),
                (4, 10, "места 4-10"),
                (11, None, "место 11 и дальше"))


async def park_born_at(session):
    """Когда создан нынешний парк ботов.

    `new_bots.py` пересоздаёт всех разом, поэтому минимальный `created_at`
    активных ботов — это и есть граница, за которой номера означали других
    ботов.
    """
    return (
        await session.execute(
            select(func.min(TestBot.created_at)).where(TestBot.is_active)
        )
    ).scalar()


async def copybot_combinations(session):
    """Различные наборы параметров отбора среди активных копиботов v1.

    Отбор зависит только от тройки (окно, проверка за сутки, отсев
    провалившихся доноров), а копиботов с каждой тройкой по одному. Считаем на тройку, а
    не на бота, чтобы не гонять один и тот же запрос по многу раз.
    """
    rows = (
        await session.execute(
            select(
                TestBot.copy_bot_min_time_profitability_min,
                TestBot.copybot_v1_check_for_24h_profitability,
                TestBot.copybot_v1_exclude_losing_donors,
            )
            .where(
                TestBot.is_active,
                TestBot.copy_bot_min_time_profitability_min.is_not(None),
            )
            .distinct()
        )
    ).all()

    return [(int(tf), bool(check_24h), bool(exclude_losing))
            for tf, check_24h, exclude_losing in rows]


def classify(row):
    """Разряд, в который попадает одна строка свёртки."""
    if row.donor_rank is not None:
        for low, high, title in RANK_BUCKETS:
            if row.donor_rank >= low and (high is None or row.donor_rank <= high):
                return title

    if row.expected_bot_id is None:
        return "донора не было — воркер оставил прежний ключ"

    return "не в отборе — взятый донор был убыточен"


def share(part, whole):
    return f"{part / whole * 100:5.1f}%" if whole else "    — "


async def run(days=None, hours=None, details=False, top_count=10):
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        rollup_crud = TestOrderRollupCrud(session)

        watermark = await rollup_crud.rollup_watermark()

        if watermark is None:
            print(
                "Свёрток ещё нет — считать не по чему.\n"
                "Свёртки делает app.tasks.test_order_rollup раз в десять "
                "минут; проверьте celery-beat и celery-worker."
            )
            return

        born = await park_born_at(session)

        if born is None:
            print("Активных ботов нет — парк не создан.")
            return

        window = timedelta(days=days or 0, hours=hours or 0)
        since = max(datetime.now(UTC) - window, born) if window else born

        print("\n📋 Донор копибота против того, кого дал бы отбор")
        print(f"   окно: {since:%Y-%m-%d %H:%M} → {watermark:%Y-%m-%d %H:%M}")

        if window and since == born:
            # Просить глубже, чем живёт парк, можно — но выдавать чужие
            # номера ботов за свои нельзя.
            print(f"   ⚠️  запрошено глубже, чем живёт парк "
                  f"(с {born:%Y-%m-%d %H:%M}) — окно урезано")

        combinations = await copybot_combinations(session)

        if not combinations:
            print("   активных копиботов v1 нет")
            return

        print(f"   комбинаций параметров у копиботов: {len(combinations)}")

        totals = defaultdict(int)
        by_window = defaultdict(lambda: defaultdict(int))
        by_bot = defaultdict(lambda: defaultdict(int))
        orders_total = 0

        for tf, check_24h, exclude_losing in combinations:
            rows = await rollup_crud.donor_match(
                tf=tf, check_24h=check_24h, exclude_losing=exclude_losing,
                since=since, until=watermark,
            )

            for row in rows:
                # Взвешиваем сделками, а не строками свёрток: блок с одной
                # сделкой и блок с тысячей — не одно и то же.
                orders = int(row.orders_count or 0)
                verdict = classify(row)

                totals[verdict] += orders
                by_window[tf][verdict] += orders
                by_bot[row.copy_bot_id][verdict] += orders
                orders_total += orders

    if not orders_total:
        print("   сделок копиботов v1 за это окно нет")
        return

    print(f"\n   сделок в разборе: {orders_total}\n")

    order = [title for _, _, title in RANK_BUCKETS] + [
        "не в отборе — взятый донор был убыточен",
        "донора не было — воркер оставил прежний ключ",
    ]

    for title in order:
        count = totals.get(title, 0)

        if count:
            print(f"   {share(count, orders_total)}  {count:>8}  {title}")

    exact = totals.get("место 1 — точное попадание", 0)
    missed = totals.get("не в отборе — взятый донор был убыточен", 0)

    print(f"\n   точных попаданий {share(exact, orders_total)}, "
          f"сделок с убыточным донором {share(missed, orders_total)}")

    if details:
        print("\n   По окнам прибыльности:")
        for tf in sorted(by_window):
            stats = by_window[tf]
            total = sum(stats.values())
            hit = stats.get("место 1 — точное попадание", 0)
            print(f"     окно {tf:>5} мин: сделок {total:>7}, "
                  f"точных {share(hit, total)}")

        print(f"\n   Копиботы с худшей долей точных попаданий "
              f"(из тех, у кого сделок хотя бы 50):")
        ranked = sorted(
            (
                (stats.get("место 1 — точное попадание", 0) / sum(stats.values()),
                 bot_id, sum(stats.values()))
                for bot_id, stats in by_bot.items()
                if sum(stats.values()) >= 50
            )
        )

        for hit_share, bot_id, total in ranked[:top_count]:
            print(f"     бот {bot_id:>6}: сделок {total:>7}, "
                  f"точных {hit_share * 100:5.1f}%")

        if not ranked:
            print("     таких ботов нет — сделок пока слишком мало")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Сходится ли донор, которого взял копибот, с тем, кого дала бы "
            "функция отбора. Без флагов окна — с рождения парка."
        )
    )
    parser.add_argument('-d', '--days', type=int,
                        help="Глубина окна в сутках.")
    parser.add_argument('-H', '--hours', type=int,
                        help="Глубина окна в часах.")
    parser.add_argument('--details', action='store_true',
                        help="Разбивка по окнам прибыльности и по ботам.")
    parser.add_argument('-top_count', '--top_count', type=int, default=10,
                        help="Сколько ботов показать в разбивке.")

    args = parser.parse_args()

    asyncio.run(run(
        days=args.days, hours=args.hours,
        details=args.details, top_count=args.top_count,
    ))


if __name__ == "__main__":
    main()
