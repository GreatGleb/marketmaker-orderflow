"""Первичная настройка проекта после клонирования репозитория.

Запускается один раз на новом сервере, когда стек уже поднят и миграции
применены (это делает boot.sh). Порядок шагов важен:

    спеки пар  →  комиссии  →  ждём цены  →  создаём ботов

Скрипт идемпотентен: повторный запуск ничего не стирает. Пересоздание парка
ботов (оно стирает test_orders) требует явного --recreate-bots.

    python -m app.scripts.init_setup                  # обычный первый запуск
    python -m app.scripts.init_setup --skip-commissions  # без долгого шага
    python -m app.scripts.init_setup --recreate-bots     # пересоздать парк
"""
import argparse
import asyncio
import logging

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.config import settings
from app.db.base import DatabaseSessionManager
from app.db.models import AssetExchangeSpec, AssetHistory, TestBot, WatchedPair
from app.scripts.new_bots import create_bots_safely
from app.scripts.seed_binance_data import seed_binance_data
from app.scripts.seed_commission_rates import seed_commission_rates
from app.scripts.seed_watched_pairs import seed_watched_pairs
from app.scripts.simulator_flag import SimulatorIsRunning

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

UTC = timezone.utc

# Сколько ждать, пока питатель цен наберёт данные. Без свежих цен нельзя
# посчитать средний процент за тик, а значит и создать волатильных ботов.
PRICES_WAIT_TIMEOUT_SECONDS = 15 * 60
PRICES_POLL_INTERVAL_SECONDS = 15
# Ниже этого числа пар волатильному режиму почти не из чего выбирать.
PAIRS_WARNING_THRESHOLD = 10
# Сколько пар держать в watched_pair при первичной настройке.
WATCHED_PAIRS_TOP = 50


def step(number: int, title: str) -> None:
    logging.info(f"\n{'=' * 60}\nШаг {number}. {title}\n{'=' * 60}")


async def count_rows(session, model) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar()


async def wait_for_prices() -> int:
    """Ждёт, пока в asset_history появятся свежие тики.

    Возвращает число пар со свежими данными (0, если не дождались).
    """
    dsm = DatabaseSessionManager.create(settings.DB_URL)
    deadline = asyncio.get_running_loop().time() + PRICES_WAIT_TIMEOUT_SECONDS

    while True:
        async with dsm.get_session() as session:
            since = datetime.now(UTC) - timedelta(minutes=2)
            pairs = (
                await session.execute(
                    select(func.count(func.distinct(AssetHistory.symbol)))
                    .where(AssetHistory.event_time >= since)
                )
            ).scalar() or 0

        if pairs:
            logging.info(f"Цены идут: пар со свежими тиками — {pairs}.")
            return pairs

        if asyncio.get_running_loop().time() >= deadline:
            return 0

        logging.info(
            f"Свежих цен пока нет, жду {PRICES_POLL_INTERVAL_SECONDS} с... "
            f"(проверьте лог /var/log/symbols_history.log)"
        )
        await asyncio.sleep(PRICES_POLL_INTERVAL_SECONDS)


async def main(skip_commissions: bool, recreate_bots: bool) -> None:
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    step(1, "Спецификации пар Binance (tick_size, шаг лота)")
    await seed_binance_data()

    step(2, "Список отслеживаемых пар (watched_pair)")
    async with dsm.get_session() as session:
        watched_before = await count_rows(session, WatchedPair)

    if watched_before:
        logging.info(
            f"В watched_pair уже {watched_before} пар, пропускаю. "
            f"Пересобрать список по скачкам: "
            f"python -m app.scripts.seed_watched_pairs --replace"
        )
    else:
        # На чистой базе истории цен ещё нет, поэтому скрипт сам уйдёт на
        # суточную статистику Binance. Когда история накопится, список стоит
        # пересобрать — тогда он отберёт пары по резким движениям.
        await seed_watched_pairs(top=WATCHED_PAIRS_TOP)

    async with dsm.get_session() as session:
        watched = await count_rows(session, WatchedPair)
        specs = await count_rows(session, AssetExchangeSpec)

    logging.info(f"Пар в asset_exchange_specs: {specs}, в watched_pair: {watched}.")

    # Питатель цен сохраняет только watched-пары: в режимах ws и rest — из-за
    # даты-переключателя в watch_ws_and_save.py (2025-10-20, давно прошла),
    # в spot_ws — при MARKET_DATA_SPOT_SYMBOLS=watched. То есть watched_pair
    # определяет, по каким парам вообще будут цены.
    if watched < PAIRS_WARNING_THRESHOLD:
        logging.info(
            f"⚠️  В watched_pair всего {watched} пар — цены будут собираться "
            f"только по ним. Волатильным ботам будет не из чего выбирать, "
            f"да и обычным достанутся только эти пары.\n"
            f"    Что сделать: добавить пары в watched_pair, либо поставить "
            f"MARKET_DATA_SOURCE=spot_ws вместе с MARKET_DATA_SPOT_SYMBOLS=all "
            f"(тогда поток пойдёт по всем парам со спекой).\n"
            f"    Пересобрать список: "
            f"python -m app.scripts.seed_watched_pairs --replace"
        )

    step(3, "Ставки комиссии по парам")
    if skip_commissions:
        logging.info(
            "Пропущено по --skip-commissions. Пока ставки не засеяны, "
            "расчёты идут по константе из app/constants/commissions.py. "
            "Позже: python -m app.scripts.seed_commission_rates --missing"
        )
    else:
        logging.info(
            "Это самый долгий шаг: Binance отдаёт ставку по одной паре за "
            "запрос, около секунды на пару."
        )
        await seed_commission_rates(mode="missing")

    step(4, "Жду, пока питатель цен наберёт данные")
    pairs_with_prices = await wait_for_prices()

    if not pairs_with_prices:
        logging.info(
            f"❌ За {PRICES_WAIT_TIMEOUT_SECONDS // 60} минут свежих цен не "
            f"появилось. Боты не созданы — без цен нельзя посчитать средний "
            f"процент за тик. Проверьте MARKET_DATA_SOURCE в .env и лог "
            f"/var/log/symbols_history.log, затем запустите скрипт снова."
        )
        return

    step(5, "Парк тестовых ботов")
    async with dsm.get_session() as session:
        bots = await count_rows(session, TestBot)

    if bots and not recreate_bots:
        logging.info(
            f"В test_bots уже {bots} ботов, создание пропущено. "
            f"Пересоздать (сотрёт и test_orders): "
            f"python -m app.scripts.init_setup --recreate-bots"
        )
    else:
        if bots:
            logging.info(f"Пересоздаю парк: было {bots} ботов, они будут стёрты.")
        # Сама обёртка останавливает симулятор на время пересоздания и
        # запускает обратно.
        await create_bots_safely()

        async with dsm.get_session() as session:
            bots = await count_rows(session, TestBot)

        logging.info(f"Ботов в базе: {bots}.")

    step(6, "Готово")
    logging.info(
        "Когда накопится история цен (сутки), пересоберите список пар по\n"
        "резким движениям — на чистой базе он взят из суточной статистики:\n"
        "  python -m app.scripts.seed_watched_pairs --replace\n\n"
        "Что проверить:\n"
        "  supervisorctl status                        — все процессы running\n"
        "  redis-cli keys 'price:*' | head             — цены идут\n"
        "  redis-cli keys 'most_volatile_symbol_*'     — пары для волатильных ботов\n"
        "  psql -c 'select count(*) from test_orders'  — сделки появляются\n"
        "  python -m app.scripts.top_bots_report -H 1  — отчёт по прибыльности"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Первичная настройка проекта после клонирования."
    )
    parser.add_argument(
        '--skip-commissions', action='store_true',
        help="Не засевать ставки комиссии (самый долгий шаг)."
    )
    parser.add_argument(
        '--recreate-bots', action='store_true',
        help="Пересоздать парк ботов, даже если он не пуст. Сотрёт test_orders."
    )
    args = parser.parse_args()

    try:
        asyncio.run(
            main(
                skip_commissions=args.skip_commissions,
                recreate_bots=args.recreate_bots,
            )
        )
    except SimulatorIsRunning as e:
        # Первичная настройка идёт под остановленным симулятором. Если его
        # остановить нечем, шаги дальше меняли бы данные под живым процессом:
        # лучше оборваться здесь, чем доделать половину.
        logging.error(str(e))
        raise SystemExit(1)
