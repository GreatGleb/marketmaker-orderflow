"""Отчёт «топ прибыльных ботов» за произвольное окно.

Главное здесь — источник данных. Сырые сделки живут
`RETENTION_TEST_ORDERS_HOURS` (72 часа по умолчанию), поэтому отчёт по
`test_orders` за неделю вернул бы цифры за трое суток — молча, без единой
ошибки, с честным заголовком «за неделю». Поэтому считаем по двум
источникам сразу: свёртки за всё, что уже свёрнуто, сырые сделки за хвост
после границы свёрнутого. Стыковка — в `TestOrderRollupCrud.profit_by_bot`.

Глубина отчёта — вся история свёрток. Запросить окно глубже, чем есть
данные, не ошибка: отчёт покажет настоящее окно, а не запрошенное, и скажет
об этом.

Без флагов окна печатает три периода сразу — сутки, неделю, две недели:

    python -m app.scripts.top_bots_report

Одно окно и только копиботы:

    python -m app.scripts.top_bots_report -d 7 -just_copy 1 -top_count 20
"""
import argparse
import asyncio

from datetime import datetime, timedelta, timezone

from app.config import settings
from app.crud.test_order_rollup import TestOrderRollupCrud
from app.db.base import DatabaseSessionManager

UTC = timezone.utc

# То, ради чего отчёт обычно и запускается. Печатаются все три, когда окно
# не задано явно: сравнивать сутки с неделей глазами в одном выводе удобнее,
# чем гонять скрипт трижды.
DEFAULT_PERIODS = (
    ("сутки", timedelta(days=1)),
    ("неделю", timedelta(days=7)),
    ("две недели", timedelta(days=14)),
)


async def print_report(
    rollup_crud: TestOrderRollupCrud,
    title: str,
    since: datetime,
    earliest: datetime,
    filters: dict,
    top_count: int,
) -> None:
    now = datetime.now(UTC)

    # Просить глубже, чем есть данные, можно — но выдавать это за настоящее
    # окно нельзя: заголовок «за месяц» над цифрами за две недели ровно
    # тем и плох, что выглядит правдоподобно.
    truncated = since < earliest

    rows, window_start, watermark = await rollup_crud.profit_by_bot(
        since=max(since, earliest), **filters
    )

    hours = (now - window_start).total_seconds() / 3600

    print(f"\n📊 Топ-{top_count} по прибыли — за {title}")
    print(
        f"   окно: {window_start:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M} "
        f"({hours:.1f} ч)"
    )

    # Из чего сложились цифры. Не украшение: если свёртки встали, граница
    # застынет в прошлом, и по этой строке это видно сразу.
    if watermark is None:
        print(
            "   источник: только сырые сделки — свёрток ещё нет, глубже "
            f"{settings.RETENTION_TEST_ORDERS_HOURS:.0f} ч данных не будет"
        )
    elif watermark <= window_start:
        print("   источник: только сырые сделки (окно целиком свежее свёрток)")
    else:
        print(
            f"   источник: свёртки до {watermark:%Y-%m-%d %H:%M}, "
            "сырые сделки после"
        )

    if truncated:
        print(
            f"   ⚠️  запрошено с {since:%Y-%m-%d %H:%M}, но данных столько "
            "нет — окно урезано до показанного"
        )

    if not rows:
        print("   сделок за это окно нет")
        return

    print()

    for place, row in enumerate(rows[:top_count], 1):
        orders = int(row.orders_count or 0)
        profitable = int(row.profitable_count or 0)
        share = profitable / orders * 100 if orders else 0

        print(
            f"{place:>3}. Бот {row.bot_id} — "
            f"💰 P/L {float(row.profit_loss or 0):.4f}, "
            f"📈 прибыльных {profitable}/{orders} ({share:.1f}%), "
            f"комиссия {float(row.fee or 0):.4f}, "
            f"закрытий [цель {int(row.stop_won or 0)} / "
            f"стоп {int(row.stop_loosed or 0)} / "
            f"время {int(row.stop_long_lose or 0)}]"
        )


async def run(
    days: int = None,
    hours: int = None,
    minutes: int = None,
    all_history: bool = False,
    just_copy_bots: str = None,
    just_copy_bots_v2: str = None,
    just_copy_bots_v3: str = None,
    just_not_copy_bots: str = None,
    by_referral: bool = False,
    top_count: int = 10,
) -> None:
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        rollup_crud = TestOrderRollupCrud(session)

        earliest = await rollup_crud.earliest_data_at()

        if earliest is None:
            print("Сделок в базе нет — ни сырых, ни свёрнутых")
            return

        filters = {
            "just_copy_bots": just_copy_bots,
            "just_copy_bots_v2": just_copy_bots_v2,
            "just_copy_bots_v3": just_copy_bots_v3,
            "just_not_copy_bots": just_not_copy_bots,
            "by_referral_bot_id": by_referral,
        }

        now = datetime.now(UTC)
        window = build_window(days=days, hours=hours, minutes=minutes)

        if all_history:
            periods = [("всю сохранённую историю", now - earliest)]
        elif window:
            periods = [(describe(window), window)]
        else:
            periods = list(DEFAULT_PERIODS)

        for title, length in periods:
            await print_report(
                rollup_crud=rollup_crud,
                title=title,
                since=now - length,
                earliest=earliest,
                filters=filters,
                top_count=top_count,
            )


def build_window(days=None, hours=None, minutes=None) -> timedelta | None:
    """Окно из флагов. `None` — окно не задано, печатаем стандартные три."""
    if days is None and hours is None and minutes is None:
        return None

    window = timedelta(
        days=days or 0, hours=hours or 0, minutes=minutes or 0
    )

    return window or None


def describe(window: timedelta) -> str:
    minutes = window.total_seconds() / 60

    if minutes >= 1440:
        return f"{minutes / 1440:.10g} сут"
    if minutes >= 60:
        return f"{minutes / 60:.10g} ч"

    return f"{minutes:.10g} мин"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Статистика прибыльности ботов за окно. Без флагов окна — "
            "сутки, неделя и две недели сразу."
        )
    )
    parser.add_argument('-d', '--days', type=int,
                        help="Глубина окна в сутках (например, 14).")
    parser.add_argument('-H', '--hours', type=int,
                        help="Глубина окна в часах (например, 24).")
    parser.add_argument('-m', '--minutes', type=int,
                        help="Глубина окна в минутах (например, 30).")
    parser.add_argument('-all', '--all_history', action='store_true',
                        help="За всю сохранённую историю.")
    parser.add_argument('-just_copy', '--just_copy_bots', type=str,
                        help="Только копиботы v1")
    parser.add_argument('-just_copy_v2', '--just_copy_bots_v2', type=str,
                        help="Только копиботы v2")
    parser.add_argument('-just_copy_v3', '--just_copy_bots_v3', type=str,
                        help="Только копиботы v3")
    parser.add_argument('-just_not_copy', '--just_not_copy_bots', type=str,
                        help="Только обычные боты")
    parser.add_argument('-ref', '--by_referral', action='store_true',
                        help="Считать по донорам, а не по самим ботам")
    parser.add_argument('-top_count', '--top_count', type=int, default=10,
                        help="Количество ботов")

    args = parser.parse_args()

    print("⏳ Считаем статистику ботов...")
    asyncio.run(
        run(
            days=args.days,
            hours=args.hours,
            minutes=args.minutes,
            all_history=args.all_history,
            just_copy_bots=args.just_copy_bots,
            just_copy_bots_v2=args.just_copy_bots_v2,
            just_copy_bots_v3=args.just_copy_bots_v3,
            just_not_copy_bots=args.just_not_copy_bots,
            by_referral=args.by_referral,
            top_count=args.top_count,
        )
    )


if __name__ == "__main__":
    main()
