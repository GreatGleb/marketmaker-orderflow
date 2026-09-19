import argparse
import logging
from collections import Counter
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select, func

from app.constants.demo_seed import copybot_seed_groups, percentage_bot_rows
from app.crud.asset_history import AssetHistoryCrud
from app.db.base import DatabaseSessionManager
from app.crud.strategy import StrategyCrud
from app.constants.strategy import STRATEGY_LEGACY
from app.crud.test_bot import TestBotCrud, bot_identity, with_strategy
from app.config import settings
import asyncio

from app.db.models import AssetExchangeSpec, AssetHistory
from app.scripts.simulator_flag import SimulatorIsRunning
from app.scripts.supervisor_control import paused

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

async def get_average_percentage_for_minimum_tick():
    """Средний процент, который стоит один тик, по всем активным парам.

    Возвращает None, если посчитать не по чему: вызывающий код обязан это
    обработать, а не подставлять значение по умолчанию. Через это число
    задаются проценты волатильных ботов, и ошибка в нём тихо перекосит все
    их стопы и тейки.
    """
    start_time = time.time()

    average_percent = None

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with (dsm.get_session() as session):
        asset_crud = AssetHistoryCrud(session)

        active_symbols = await asset_crud.get_all_active_pairs(only_symbols_in_period=True)

        if not active_symbols:
            print('Нет свежих цен в asset_history — средний процент за тик '
                  'посчитать не по чему.')
            return None

        stmt_active_symbols = (
            select(AssetExchangeSpec.symbol)
            .where(AssetExchangeSpec.symbol.in_(active_symbols))
        )
        result_symbols = await session.execute(stmt_active_symbols)

        actual_active_symbols = {s[0] for s in result_symbols.all()}

        subquery_ranked_prices = (
            select(
                AssetHistory.id,
                AssetHistory.symbol,
                AssetHistory.created_at,
                AssetHistory.last_price,
                func.row_number()
                .over(
                    partition_by=AssetHistory.symbol,
                    order_by=AssetHistory.created_at.desc()
                )
                .label("rn")
            )
            .cte("ranked_prices")
        )

        stmt_latest_prices = (
            select(
                AssetHistory.symbol,
                AssetExchangeSpec.filters,
                subquery_ranked_prices.c.last_price,
            )
            .join(AssetHistory, AssetHistory.id == subquery_ranked_prices.c.id)
            .join(AssetExchangeSpec, AssetExchangeSpec.symbol == AssetHistory.symbol)
            .where(subquery_ranked_prices.c.rn == 1)
            .where(AssetExchangeSpec.symbol.in_(actual_active_symbols))
        )

        result_latest_prices = await session.execute(stmt_latest_prices)
        all_latest_prices_for_active_symbols = result_latest_prices.all()

        percents = []
        symbols_characteristics = {}
        for symbol, filters, last_price in all_latest_prices_for_active_symbols:
            if not filters:
                continue

            tick_size = Decimal(filters[0]['tickSize'])

            percent = (1/(last_price/tick_size)) * 100
            percents.append(percent)

            symbols_characteristics[symbol] = [tick_size, last_price]

        if not percents:
            print('Ни у одной активной пары нет PRICE_FILTER — средний '
                  'процент за тик посчитать не из чего.')
            return None

        sum_of_percents = sum(percents)
        average_percent = sum_of_percents / len(percents)

        end_time = time.time()
        elapsed_time = end_time - start_time

        minutes = int(elapsed_time // 60)
        seconds = elapsed_time % 60

        print(f"Время, чтобы узнать средний процент по 1 тику: {minutes} минут и {seconds:.2f} секунд")

        print(f'average_percent: {average_percent}')

    return average_percent

async def get_volatile_symbols(session):
    asset_crud = AssetHistoryCrud(session)
    active_symbols = await asset_crud.get_all_active_pairs(is_need_full_info=True)

    if not active_symbols:
        return []

    filtered_symbols = [
        symbol for symbol in active_symbols
        if float(symbol.quote_asset_volume_24h) > 2000000
    ]

    sorted_symbols = sorted(
        filtered_symbols,
        key=lambda x: float(x.price_change_percent_24h),
        reverse=True
    )

    count = len(sorted_symbols)
    top_10_percent_count = int(count * 0.1)
    top_symbols = [
        symbol.symbol for symbol in sorted_symbols[:top_10_percent_count]
    ]

    now = datetime.now(timezone.utc)
    time_ago = now - timedelta(hours=6)

    most_volatiles_h6 = await asset_crud.get_most_volatiles_since_from_symbols_list(
        since=time_ago,
        symbols_list=top_symbols
    )

    time_ago = now - timedelta(hours=3)

    most_volatiles_h3 = await asset_crud.get_most_volatiles_since_from_symbols_list(
        since=time_ago,
        symbols_list=most_volatiles_h6
    )

    most_volatiles_h3_6 = most_volatiles_h3[:8]

    return most_volatiles_h3_6

async def deactivate_not_profit_bots(bot_crud):
    bot_symbols = await bot_crud.get_bot_symbols()
    need_to_deactivate_bots = []

    since_timedelta = timedelta(hours=12)

    for symbol in bot_symbols:
        profits_data = await bot_crud.get_sorted_by_profit(
            since=since_timedelta,
            just_not_copy_bots=True,
            symbol=symbol
        )

        # Боты без сделок и сделки с пустым profit_loss дают строки с None:
        # без отсева sorted() падает на сравнении None с числом.
        rows_with_profit = [
            row for row in profits_data if row[1] is not None
        ]
        sorted_data = sorted(
            rows_with_profit, key=lambda x: x[1], reverse=True
        )

        if not sorted_data:
            # Нет сделок за 12 часов — это не то же самое, что убыточная пара.
            # Отключать по молчанию нельзя: пару могли только что завести или
            # симулятор стоял. Пропускаем.
            print(f'Symbol: {symbol}, за 12 часов нет сделок — пропускаю')
            continue

        max_profit = sorted_data[0][1]

        if max_profit < 100:
            need_to_deactivate_bots.append(symbol)

        print(f'Symbol: {symbol}, max profit for 12 hours: {max_profit}')

    print('deactivating bots...')

    for symbol in need_to_deactivate_bots:
        print(f'deactivating {symbol}')
        await bot_crud.deactivate_bot(symbol)

    print(bot_symbols)
    print('bot_symbols')
    print(need_to_deactivate_bots)
    print('need_to_deactivate_bots')


async def create_bots(dry_run: bool = False, strategy: str = STRATEGY_LEGACY,
                      replace: bool = False):
    """Досевает парк стратегии: 2000 процентных ботов и копиботы.

    Повторный запуск не плодит дублей: сверка идёт по ключу
    конфигурации (`bot_identity`), и заводятся только недостающие боты.

    `replace=True` — замена набора: прежние конфигурации этой стратегии
    деактивируются, новые заводятся с новыми id. Сделки остаются на
    месте, поэтому накопленная статистика переживает пересборку.
    Чужие стратегии не трогаются ни в одном из случаев.

    `TRUNCATE` здесь больше нет. Он сносил `test_bots` вместе с
    `test_orders` по внешнему ключу — то есть всю историю эксперимента,
    — и не давал завести парк второй стратегии, не разрушив первый.
    """
    # Данные и сетку проверяем до записи: сбой не должен трогать парк.
    average_percent = await get_average_percentage_for_minimum_tick()
    if average_percent is None:
        raise RuntimeError(
            'Не посчитан средний процент за тик — запустите '
            'app.scripts.watch_ws_and_save, дайте ему набрать цены и повторите'
        )
    rows = percentage_bot_rows(Decimal(str(average_percent)))
    copybot_groups = copybot_seed_groups()
    print(f'Процентных ботов: {len(rows)}, средний процент за тик: {average_percent}')
    for field in (
        'start_updown_percents', 'stop_loss_percents', 'stop_win_percents',
        'min_timeframe_asset_volatility',
    ):
        counts = Counter(row[field] for row in rows)
        print(f'{field}: ' + ', '.join(
            f'{value}: {count}' for value, count in sorted(counts.items())
        ))
    for version, bots in copybot_groups.items():
        print(f'Копиботов {version}: {len(bots)}')
    print(f'Всего ботов: {len(rows) + sum(map(len, copybot_groups.values()))}')
    if dry_run:
        return

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    async with dsm.get_session() as session:
        strategy_crud = StrategyCrud(session)
        strategy_id = await strategy_crud.ensure(strategy, strategy)
        bot_crud = TestBotCrud(session)

        deactivated = 0
        if replace:
            deactivated = await bot_crud.deactivate_strategy(strategy_id)

        # После деактивации в парке не осталось активных конфигураций
        # этой стратегии, поэтому сверка идёт только при досеве.
        known = set() if replace else await bot_crud.existing_identities(
            strategy_id
        )

        created = {}

        for name, group in (("процентных", rows), *copybot_groups.items()):
            fresh = [row for row in group if bot_identity(row) not in known]
            prepared = with_strategy(fresh, strategy_id)

            for offset in range(0, len(prepared), 250):
                await bot_crud.bulk_create(prepared[offset:offset + 250])

            created[name] = len(prepared)

        await session.commit()

    if replace:
        print(f"✅ Деактивировано прежних ботов: {deactivated}")

    for name, count in created.items():
        if count:
            print(f"✅ Ботов {name} создано: {count}")
        else:
            print(f"   Ботов {name} досевать не потребовалось")


async def create_bots_safely(dry_run: bool = False,
                             strategy: str = STRATEGY_LEGACY,
                             replace: bool = False):
    """create_bots с остановкой симулятора.

    Состав парка симулятор читает один раз на старте, поэтому менять его
    под работающими процессами бессмысленно: новых ботов они не увидят, а
    деактивированных продолжат считать активными до перезапуска. Открытые
    позиции живут только в памяти процесса и при остановке теряются — это
    меньшее зло по сравнению с расхождением между парком и тем, что в
    памяти у симулятора.
    """
    if dry_run:
        await create_bots(dry_run=True, strategy=strategy, replace=replace)
        return
    with paused("test_bots"):
        await create_bots(strategy=strategy, replace=replace)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Досеять парк стратегии: 2000 процентных демоботов и копиботы. "
            "Повторный запуск не плодит дублей."
        )
    )
    parser.add_argument("--dry-run", action="store_true", help="Показать распределение без записи")
    parser.add_argument("--strategy", default=STRATEGY_LEGACY,
                        help="Ключ стратегии, чей парк засевается")
    parser.add_argument("--replace", action="store_true",
                        help=(
                            "Заменить набор: прежние боты этой стратегии "
                            "деактивируются, история сделок остаётся"
                        ))
    args = parser.parse_args()
    try:
        asyncio.run(create_bots_safely(
            dry_run=args.dry_run, strategy=args.strategy, replace=args.replace,
        ))
    except SimulatorIsRunning as e:
        # Текст исключения — готовое сообщение человеку, трейсбек тут лишний.
        logging.error(str(e))
        raise SystemExit(1)
