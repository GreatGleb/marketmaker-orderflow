"""Отчёт по копиботам v3 — прогноз результата боевого бота.

Копибот v3 ходит той же цепочкой, что и боевой `binance_bot`: лучший копибот
v2 за своё окно, по его окну лучший v1, по фильтрам того — обычный бот, чьим
конфигом и торгуется сделка. Поэтому его сделки и есть прогноз реальной
торговли.

Ботов двое, и читать их надо вместе:

* с фиксированным балансом — 1000 на каждую сделку, как у всего парка. Его
  цифры сравнимы с `top_bots_report`;
* с компаундингом — ведёт счёт, в позицию идёт 99% баланса, количество
  округляется по шагу лота. Это прогноз в деньгах, и только он показывает,
  где реальный бот упрётся в минимальный лот и остановится.

Источник — `TestOrderRollupCrud`: свёртки за всё, что уже свёрнуто, сырьё за
хвост. По одному `test_orders` окно глубже `RETENTION_TEST_ORDERS_HOURS`
молча обрезалось бы до срока хранения сырья.

    python -m app.scripts.v3_forecast_report          # сутки, неделя
    python -m app.scripts.v3_forecast_report -d 3     # своё окно
    python -m app.scripts.v3_forecast_report -all     # вся история

Чего отчёт не знает: проскальзывания и реального исполнения стоп-маркет
ордеров. Симулятор считает, что вход случился по цене пробоя, а выход ровно по
уровню, поэтому цифры — верхняя оценка. Померить разницу можно будет только
сверкой с `market_orders`, когда боевой бот отработает.
"""
import argparse
import asyncio

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import settings
from app.crud.test_bot import TestBotCrud
from app.crud.test_order_rollup import TestOrderRollupCrud
from app.db.base import DatabaseSessionManager

UTC = timezone.utc

DEFAULT_PERIODS = (
    ("сутки", timedelta(days=1)),
    ("неделю", timedelta(days=7)),
)


def equity_curve(series, start_balance: Decimal) -> list[Decimal]:
    """Счёт на начало окна и после каждого блока времени.

    Складывать PnL можно и для компаундирующего бота: его сделки уже
    посчитаны от номинала, который в тот момент зависел от счёта, поэтому
    сумма результатов — и есть счёт.

    Первой точкой идёт сам `start_balance`. Без неё просадка первого блока
    считалась бы от уже упавшего счёта, то есть терялась бы целиком, а
    подпись «счёт X → Y» показывала бы вместо начала окна результат первой
    же сделки.
    """
    balance = Decimal(start_balance)
    curve = [balance]

    for row in series:
        balance = balance + Decimal(str(row.profit_loss or 0))
        curve.append(balance)

    return curve


def max_drawdown(curve: list[Decimal]) -> tuple[Decimal, Decimal]:
    """Наибольшее падение от пика, в деньгах и в долях пика.

    Считается по всей кривой, а не от начального баланса: просадка после
    удачной серии — это потеря заработанного, и для решения «запускать или
    нет» она важнее, чем расстояние до стартовой тысячи.
    """
    if not curve:
        return Decimal(0), Decimal(0)

    peak = curve[0]
    worst = Decimal(0)
    worst_share = Decimal(0)

    for value in curve:
        if value > peak:
            peak = value

        drop = peak - value

        if drop > worst:
            worst = drop
            worst_share = drop / peak if peak > 0 else Decimal(0)

    return worst, worst_share


def build_window(days=None, hours=None) -> timedelta | None:
    if days is None and hours is None:
        return None

    window = timedelta(days=days or 0, hours=hours or 0)

    return window or None


def describe(window: timedelta) -> str:
    hours = window.total_seconds() / 3600

    if hours >= 24:
        return f"{hours / 24:.10g} сут"

    return f"{hours:.10g} ч"


def bot_title(bot) -> str:
    kind = (
        'компаундирующий, 99% счёта'
        if bot.copybot_v3_compound_balance
        else 'фиксированный баланс 1000'
    )

    return f"Бот {bot.id} ({kind}, окно {bot.copybot_v3_time_in_minutes:.10g} мин)"


async def print_bot(
    rollup_crud: TestOrderRollupCrud,
    bot,
    stats,
    since: datetime,
    hours: float,
) -> None:
    print(f"\n  {bot_title(bot)}")

    if bot.copybot_v3_stopped_at:
        # Главный результат прогноза, если он случился: реальный бот в этот
        # момент перестал бы торговать.
        print(
            f"    🛑 остановлен {bot.copybot_v3_stopped_at:%Y-%m-%d %H:%M} — "
            f"на балансе перестал набираться минимальный лот"
        )

    if stats is None or not int(stats.orders_count or 0):
        print("    сделок за это окно нет")
        return

    orders = int(stats.orders_count or 0)
    profitable = int(stats.profitable_count or 0)
    share = profitable / orders * 100 if orders else 0
    pnl = Decimal(str(stats.profit_loss or 0))
    per_day = orders / (hours / 24) if hours else 0

    series = await rollup_crud.profit_series(bot_id=bot.id, since=since)

    # Счёт на начало окна, а не стартовая тысяча. У компаундирующего бота
    # `test_bots.balance` — это «сколько сейчас», и если окно не с рождения
    # бота, начинать кривую с тысячи значило бы показывать чужие деньги:
    # и просадка в долях пика, и подпись «счёт X → Y» считались бы от не той
    # величины. Вычитаем результат окна из текущего остатка и получаем то, с
    # чем бот в это окно вошёл.
    #
    # У бота с фиксированным балансом накопленного счёта нет вовсе: каждая его
    # сделка считается на ту же тысячу. Для него кривая — это «сколько
    # набежало бы, если реинвестировать», и начинается она с тысячи.
    if bot.copybot_v3_compound_balance:
        start_balance = Decimal(str(bot.balance)) - pnl
    else:
        start_balance = Decimal("1000")

    curve = equity_curve(series, start_balance)
    drop, drop_share = max_drawdown(curve)

    print(
        f"    💰 P/L {float(pnl):.4f}, комиссия {float(stats.fee or 0):.4f}"
    )

    if curve:
        print(
            f"    📈 счёт {float(curve[0]):.2f} → {float(curve[-1]):.2f} "
            f"за окно"
        )

    print(
        f"    📊 сделок {orders} ({per_day:.1f} в сутки), "
        f"прибыльных {profitable} ({share:.1f}%)"
    )
    print(
        f"    📉 максимальная просадка {float(drop):.2f} "
        f"({float(drop_share) * 100:.1f}% от пика)"
    )
    print(
        f"    закрытий [цель {int(stats.stop_won or 0)} / "
        f"стоп {int(stats.stop_loosed or 0)} / "
        f"время {int(stats.stop_long_lose or 0)}]"
    )


async def print_report(
    rollup_crud: TestOrderRollupCrud,
    bots,
    title: str,
    since: datetime,
    earliest: datetime,
) -> None:
    now = datetime.now(UTC)
    truncated = since < earliest

    rows, window_start, watermark = await rollup_crud.profit_by_bot(
        since=max(since, earliest), just_copy_bots_v3=True
    )

    stats_by_bot = {row.bot_id: row for row in rows}
    hours = (now - window_start).total_seconds() / 3600

    print(f"\n📊 Копиботы v3 — прогноз за {title}")
    print(
        f"   окно: {window_start:%Y-%m-%d %H:%M} → {now:%Y-%m-%d %H:%M} "
        f"({hours:.1f} ч)"
    )

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

    for bot in bots:
        await print_bot(
            rollup_crud=rollup_crud,
            bot=bot,
            stats=stats_by_bot.get(bot.id),
            since=window_start,
            hours=hours,
        )


async def run(
    days: int = None,
    hours: int = None,
    all_history: bool = False,
) -> None:
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        bots = await TestBotCrud(session).get_copybots_v3()

        if not bots:
            print(
                "Копиботов v3 в базе нет. Завести: "
                "python -m app.scripts.seed_copybot_v3"
            )
            return

        rollup_crud = TestOrderRollupCrud(session)
        earliest = await rollup_crud.earliest_data_at()

        if earliest is None:
            print("Сделок в базе нет — ни сырых, ни свёрнутых")
            return

        # Фиксированный первым: его цифры сравнимы с остальным парком, и
        # читать компаундирующего проще, уже зная их.
        bots = sorted(bots, key=lambda bot: bot.copybot_v3_compound_balance)

        now = datetime.now(UTC)
        window = build_window(days=days, hours=hours)

        if all_history:
            periods = [("всю сохранённую историю", now - earliest)]
        elif window:
            periods = [(describe(window), window)]
        else:
            periods = list(DEFAULT_PERIODS)

        for title, length in periods:
            await print_report(
                rollup_crud=rollup_crud,
                bots=bots,
                title=title,
                since=now - length,
                earliest=earliest,
            )

        print(
            "\nℹ️  Цифры — верхняя оценка: проскальзывание и реальное "
            "исполнение стоп-маркет ордеров в симуляторе не моделируются."
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Прогноз результата боевого бота по сделкам копиботов v3. "
            "Без флагов окна — сутки и неделя."
        )
    )
    parser.add_argument('-d', '--days', type=int,
                        help="Глубина окна в сутках (например, 3).")
    parser.add_argument('-H', '--hours', type=int,
                        help="Глубина окна в часах (например, 12).")
    parser.add_argument('-all', '--all_history', action='store_true',
                        help="За всю сохранённую историю.")

    args = parser.parse_args()

    print("⏳ Считаем прогноз по копиботам v3...")
    asyncio.run(
        run(
            days=args.days,
            hours=args.hours,
            all_history=args.all_history,
        )
    )


if __name__ == "__main__":
    main()
