"""Наполняет watched_pair парами с самыми резкими скачками цены.

Питатель цен сохраняет котировки только по парам из watched_pair, поэтому
именно этот список определяет, по чему вообще торгуют тестовые боты и из чего
выбирает волатильный режим.

Отбор идёт по скачкам, а не по разбросу: пара, которая за 6 часов плавно
уползла на 9%, скальперу бесполезна, а та, что десять раз дёрнулась на 1% за
секунду, — то что нужно.

Сами скачки от мусора не защищают, поэтому в отборе стоят отсечки: оборот от
$2M за сутки и не меньше двух подтверждённых скачков (один — это чаще всего
битый тик). После них пар набирается меньше, чем просили, и остаток
добирается по суточному размаху — см. top_up_from_binance.

Ещё до всяких отсечек круг сужается до инструментов, которыми мы умеем
торговать: бессрочные контракты с котировкой в USDT или USDC
(`app/constants/markets.py`). Это примерно четверть суточной статистики
Binance — в основном полторы сотни токенизированных акций (AAPLUSDT,
NVDAUSDT), которые котируются в USDT, по имени от крипты неотличимы и при
этом стоят большую часть суток. Плюс поставочные квартальные и мелочь
в чужой котировке (ETHBTC, BTCUSD1).

Курица и яйцо: скачки считаются по локальной истории, а история собирается
только по watched-парам. На чистой базе истории нет вообще, поэтому там
работает разгон:

    1. берём 100 самых волатильных пар за сутки по статистике Binance;
    2. кладём их в watched_pair и перезапускаем питатель цен;
    3. 5 минут собираем по ним настоящие тики;
    4. считаем скачки за эти 5 минут и оставляем топ-50.

То есть суточная статистика нужна только чтобы сузить круг кандидатов,
а решает всё равно реальное поведение цены.

    python -m app.scripts.seed_watched_pairs                 # топ-50 по скачкам
    python -m app.scripts.seed_watched_pairs --replace       # заменить список
    python -m app.scripts.seed_watched_pairs --bootstrap     # разгон с нуля
    python -m app.scripts.seed_watched_pairs --bootstrap --watch-minutes 15
"""
import argparse
import asyncio
import logging

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx

from sqlalchemy import delete, func, select

from app.config import settings
from app.constants.markets import (
    TRADABLE_CONTRACT_TYPES,
    TRADABLE_QUOTE_ASSETS,
)
# Тот же порог оборота, что и у отбора волатильной пары: держать два своих
# понятия неликвида в одном проекте — верный способ их разъехать.
from app.constants.volatility import MIN_QUOTE_VOLUME_24H
from app.crud.asset_history import AssetHistoryCrud
from app.crud.test_bot import TestBotCrud
from app.db.base import DatabaseSessionManager
from app.db.models import AssetExchangeSpec, AssetHistory, WatchedPair
from app.scripts.simulator_flag import SimulatorIsRunning
from app.scripts.supervisor_control import is_running, paused, restart

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

UTC = timezone.utc

BINANCE_24H_URL = "https://fapi.binance.com/fapi/v1/ticker/24hr"
# Разгон на чистой базе: сколько кандидатов взять и сколько минут за ними
# следить, прежде чем считать скачки.
BOOTSTRAP_CANDIDATES = 100
BOOTSTRAP_WATCH_MINUTES = 5
BOOTSTRAP_PROGRESS_INTERVAL_SECONDS = 30


@asynccontextmanager
async def paused_simulator():
    """paused() из supervisor_control, пригодный для async with.

    Нужен только чтобы не плодить уровни отступов в seed_watched_pairs.
    """
    with paused("test_bots"):
        yield


async def symbol_reference(session):
    """Справочник пар двумя множествами: (все символы, торгуемые из них).

    Оба нужны порознь, потому что кандидат отваливается по двум разным
    причинам, и путать их нельзя: «пара не в USDT/USDC» — это норма, а «пары
    нет в справочнике» означает, что asset_exchange_specs протух и
    seed_binance_data пора выполнить заново. В слитом виде вторая причина
    молчала бы за первой.

    Одним запросом на весь прогон: строк меньше тысячи, и держать их
    множеством дешевле, чем гонять `in_` со списком кандидатов на каждом
    этапе отбора.

    Что именно считается торгуемым — в app/constants/markets.py.
    """
    rows = (
        await session.execute(
            select(
                AssetExchangeSpec.symbol,
                AssetExchangeSpec.quote_asset,
                AssetExchangeSpec.contract_type,
            )
        )
    ).all()

    known = {row.symbol for row in rows}
    tradable = {
        row.symbol for row in rows
        if row.quote_asset in TRADABLE_QUOTE_ASSETS
        and row.contract_type in TRADABLE_CONTRACT_TYPES
    }

    return known, tradable


async def tradable_symbols(session):
    """Только торгуемые символы — когда причина отсева не важна."""
    _, tradable = await symbol_reference(session)

    return tradable


def keep_tradable(symbols, allowed, stage):
    """Оставляет только торгуемые символы, сохраняя порядок отбора.

    Отсеянное не молчит: список пар — то, по чему потом идут сделки, и
    «пропало пятнадцать пар неизвестно почему» здесь дороже лишней строки
    в логе.
    """
    kept = [symbol for symbol in symbols if symbol in allowed]
    dropped = [symbol for symbol in symbols if symbol not in allowed]

    if dropped:
        logging.info(
            f"{stage}: отброшено {len(dropped)} пар — не бессрочные "
            f"контракты в {'/'.join(TRADABLE_QUOTE_ASSETS)} либо нет записи "
            f"в asset_exchange_specs: {', '.join(dropped[:10])}"
            f"{' и другие' if len(dropped) > 10 else ''}."
        )

    return kept


async def rank_by_jumps(session, hours, top, jump_threshold):
    """Топ пар по резким движениям в локальной истории цен."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    rows = await AssetHistoryCrud(session).get_top_jumpy_symbols(
        since=since, limit=top, jump_threshold=jump_threshold
    )

    # Обычно тут не отсекается ничего: история собирается только по
    # watched-парам, а они уже прошли этот фильтр при прошлой пересборке.
    # Нужно для случая, когда в watched_pair попало что-то мимо скрипта —
    # руками или запросом.
    allowed = await tradable_symbols(session)
    kept = set(keep_tradable([row.symbol for row in rows], allowed, "Скачки"))
    rows = [row for row in rows if row.symbol in kept]

    if not rows:
        # Скачков не нашлось. Если истории тоже нет — это случай разгона. А
        # если тики идут, просто рынок стоял, разгон не нужен: он гасит
        # симулятор и перезапускает питатель ради того же результата, что
        # даёт добор по суточному размаху.
        if not await has_ticks(session, since):
            return None

        logging.info(
            f"За {hours} ч ни одна пара не показала скачков выше порога, "
            f"но тики идут — беру суточный размах."
        )

        return await top_up_from_binance(session, [], top=top)

    logging.info(f"Топ по скачкам за {hours} ч (порог {jump_threshold}% за секунду):")
    for i, row in enumerate(rows[:15], start=1):
        logging.info(
            f"  {i:>2}. {row.symbol:<14} скачков={row.jumps:<4} "
            f"сумма={float(row.jumps_sum):6.1f}%  максимум={float(row.max_jump):5.2f}%"
        )
    if len(rows) > 15:
        logging.info(f"  ... и ещё {len(rows) - 15}")

    return await top_up_from_binance(
        session, [row.symbol for row in rows], top=top
    )


async def has_ticks(session, since):
    """Есть ли в окне хоть один тик: пустая история и тихий рынок — не одно и
    то же, а реакция на них разная."""
    return bool((
        await session.execute(
            select(AssetHistory.id).where(AssetHistory.event_time >= since).limit(1)
        )
    ).scalar())


async def top_up_from_binance(session, chosen, top):
    """Добирает список до top парами из суточной статистики Binance.

    После отсечек по обороту и числу фронтов отбор по скачкам отдаёт заметно
    меньше пар, чем просили: на августовской истории 5 из 50. Оставлять
    watched_pair из пяти пар нельзя — питатель соберёт историю только по ним, и
    следующий отбор будет выбирать из них же, всё сильнее замыкаясь.

    Разгон (bootstrap) для этого слишком дорог: он заменяет список целиком,
    перезапускает питатель и останавливает симулятор на несколько минут.
    Здесь же остаток просто добирается по суточному размаху — тот же приём,
    что и в watch_and_rank.
    """
    missing = top - len(chosen)

    if missing <= 0:
        return chosen

    logging.info(
        f"Скачки дали {len(chosen)} пар из {top} — добираю остаток "
        f"по суточной статистике."
    )

    try:
        candidates = await rank_from_binance(session, top=top + len(chosen))
    except Exception as error:
        # Сеть отвалилась — это не повод терять уже отобранное.
        logging.info(f"Добрать не удалось ({error}), оставляю {len(chosen)} пар.")
        return chosen

    already = set(chosen)
    added = [s for s in candidates if s not in already][:missing]

    logging.info(f"Добрано {len(added)} пар: {added[:10]}"
                 f"{'...' if len(added) > 10 else ''}")

    return chosen + added


async def rank_from_binance(session, top, reference=None):
    """Запасной отбор, когда локальной истории ещё нет.

    Суточная статистика скачков не показывает, поэтому берём размах
    (high-low)/low среди достаточно ликвидных пар. Это грубее, но позволяет
    начать собирать цены — а через сутки список стоит пересчитать по истории.

    Что торгуемо, решает справочник, а не имя символа: у токенизированных
    акций оно кончается на USDT ровно так же, как у крипты, и отличить
    AAPLUSDT от AAVEUSDT по суффиксу нельзя.
    """
    logging.info(f"Беру суточную статистику Binance, топ-{top} по размаху.")

    known, allowed = reference or await symbol_reference(session)

    if not allowed:
        # Пустой справочник — не «ничего не подошло», а несделанный шаг
        # установки. Без него отбор молча вернул бы ноль пар.
        logging.info(
            "В asset_exchange_specs нет ни одной подходящей пары. "
            "Сначала выполните app.scripts.seed_binance_data."
        )
        return []

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(BINANCE_24H_URL)
        response.raise_for_status()
        tickers = response.json()

    candidates = []
    untradable = 0
    unknown = []

    for ticker in tickers:
        symbol = ticker.get("symbol", "")

        if symbol not in allowed:
            if symbol in known:
                untradable += 1
            else:
                unknown.append(symbol)
            continue

        try:
            volume = float(ticker["quoteVolume"])
            high, low = float(ticker["highPrice"]), float(ticker["lowPrice"])
        except (KeyError, TypeError, ValueError):
            continue

        if volume < MIN_QUOTE_VOLUME_24H or low <= 0:
            continue

        candidates.append((symbol, (high - low) / low * 100, volume))

    candidates.sort(key=lambda item: item[1], reverse=True)
    # Отсев идёт до среза по top, иначе нетоварные пары занимали бы места в
    # топе и список выходил бы короче запрошенного.
    chosen = candidates[:top]

    logging.info(
        f"Из {len(tickers)} пар Binance пропущено {untradable} не бессрочных "
        f"в {'/'.join(TRADABLE_QUOTE_ASSETS)}, по обороту и размаху отобрано "
        f"{len(chosen)}."
    )

    if unknown:
        # Не отсев по нашим правилам, а расхождение со справочником: Binance
        # знает пару, а мы нет. Одна-две — свежие листинги, много — значит
        # seed_binance_data не гоняли давно, и отбор идёт по устаревшему
        # кругу кандидатов.
        logging.info(
            f"Ещё {len(unknown)} пар Binance нет в asset_exchange_specs, "
            f"например: {', '.join(unknown[:10])}"
            f"{' и другие' if len(unknown) > 10 else ''}. "
            f"Если их много — выполните app.scripts.seed_binance_data."
        )

    logging.info(f"Топ по суточному размаху (оборот от {MIN_QUOTE_VOLUME_24H:,}):")
    for i, (symbol, spread, volume) in enumerate(chosen[:15], start=1):
        logging.info(f"  {i:>2}. {symbol:<14} размах={spread:6.2f}%  оборот={volume:,.0f}")

    return [symbol for symbol, _, _ in chosen]


def restart_price_feed() -> None:
    """Перезапускает питатель цен, чтобы он подхватил новый список пар.

    В режиме spot_ws подписка на стримы формируется один раз при подключении
    (watch_ws_and_save.py:313), поэтому без перезапуска новые пары останутся
    без котировок. В режимах ws и rest фильтр перечитывается на каждом сбросе
    в БД, и перезапуск там просто безвреден.
    """
    restart("symbols_history")


async def watch_and_rank(candidates, top, watch_minutes, jump_threshold):
    """Следит за парами watch_minutes минут и ранжирует их по скачкам.

    Кандидаты уже должны лежать в watched_pair, иначе цены по ним не пишутся.
    Если за это время скачков нашлось меньше, чем нужно пар, добираем
    остальные в исходном порядке кандидатов — лучше так, чем вернуть список
    из трёх пар после тихих пяти минут.
    """
    started_at = datetime.now(UTC)
    seconds = int(watch_minutes * 60)

    logging.info(
        f"Слежу за {len(candidates)} парами {watch_minutes} мин, "
        f"чтобы посчитать скачки по настоящим тикам..."
    )

    dsm = DatabaseSessionManager.create(settings.DB_URL)
    waited = 0

    while waited < seconds:
        await asyncio.sleep(
            min(BOOTSTRAP_PROGRESS_INTERVAL_SECONDS, seconds - waited)
        )
        waited += BOOTSTRAP_PROGRESS_INTERVAL_SECONDS

        async with dsm.get_session() as session:
            with_data = (
                await session.execute(
                    select(func.count(func.distinct(AssetHistory.symbol)))
                    .where(AssetHistory.event_time >= started_at)
                )
            ).scalar() or 0

        logging.info(
            f"  прошло {min(waited, seconds)} из {seconds} с, "
            f"тики идут по {with_data} парам"
        )

    async with dsm.get_session() as session:
        rows = await AssetHistoryCrud(session).get_top_jumpy_symbols(
            since=started_at, limit=top, jump_threshold=jump_threshold
        )

    if rows:
        logging.info(f"Скачки за {watch_minutes} мин (порог {jump_threshold}%):")
        for i, row in enumerate(rows[:15], start=1):
            logging.info(
                f"  {i:>2}. {row.symbol:<14} скачков={row.jumps:<4} "
                f"сумма={float(row.jumps_sum):6.1f}%  максимум={float(row.max_jump):5.2f}%"
            )
    else:
        logging.info("За это время ни одного скачка выше порога не случилось.")

    chosen = [row.symbol for row in rows]

    if len(chosen) < top:
        filler = [s for s in candidates if s not in set(chosen)]
        added = filler[:top - len(chosen)]
        chosen += added
        logging.info(
            f"Скачки дали только {len(rows)} пар — добираю ещё {len(added)} "
            f"по суточной статистике."
        )

    return chosen


async def bootstrap(session, top, candidates_count, watch_minutes, jump_threshold):
    """Разгон на чистой базе: кандидаты → 5 минут наблюдения → топ по скачкам."""
    candidates = await rank_from_binance(session, top=candidates_count)

    if not candidates:
        return None

    added, removed = await apply_watched_pairs(session, candidates, replace=True)
    logging.info(
        f"В watched_pair временно положено {len(candidates)} кандидатов "
        f"(добавлено {added}, убрано {removed})."
    )

    restart_price_feed()

    return await watch_and_rank(
        candidates=candidates, top=top, watch_minutes=watch_minutes,
        jump_threshold=jump_threshold,
    )


async def get_pinned_symbols(session):
    """Пары, которые нельзя убирать из watched_pair ни при какой пересборке.

    Это пары активных ботов с закреплённым символом: пропадёт пара — пропадут
    цены по ней, и боты просто встанут. Волатильных ботов это не касается,
    у них symbol пустой, а пару они берут из Redis.
    """
    return await TestBotCrud(session).get_bot_symbols()


async def apply_watched_pairs(session, symbols, replace):
    """Кладёт выбранные пары в watched_pair. Возвращает (добавлено, удалено).

    При replace=True сначала досыпает пары активных ботов: удалять их нельзя,
    поэтому итоговый список может оказаться чуть длиннее запрошенного топа.
    Так честнее, чем тратить на них места в рейтинге.

    Здесь же последняя проверка на торгуемость. Отбор её уже прошёл, но это
    единственная запись в watched_pair во всём проекте, и держать проверку
    на входе в неё дешевле, чем полагаться на то, что все вызывающие
    отфильтровали список сами.
    """
    symbols = keep_tradable(
        symbols, await tradable_symbols(session), "Запись в watched_pair"
    )

    if replace:
        pinned = await get_pinned_symbols(session)
        missing_pinned = [s for s in pinned if s not in set(symbols)]

        if missing_pinned:
            logging.info(
                f"Досыпаю пары активных ботов, их нельзя терять: "
                f"{missing_pinned}"
            )
            symbols = list(symbols) + missing_pinned

    spec_rows = (
        await session.execute(
            select(AssetExchangeSpec.id, AssetExchangeSpec.symbol)
            .where(AssetExchangeSpec.symbol.in_(symbols))
        )
    ).all()

    spec_id_by_symbol = {symbol: spec_id for spec_id, symbol in spec_rows}
    missing = [s for s in symbols if s not in spec_id_by_symbol]

    if missing:
        logging.info(
            f"Нет записи в asset_exchange_specs у {len(missing)} пар, пропускаю: "
            f"{missing[:5]}{'...' if len(missing) > 5 else ''}. "
            f"Сначала выполните app.scripts.seed_binance_data."
        )

    wanted_ids = {spec_id_by_symbol[s] for s in symbols if s in spec_id_by_symbol}
    existing_ids = set(
        (await session.execute(select(WatchedPair.asset_exchange_id))).scalars().all()
    )

    removed = 0

    if replace:
        to_remove = existing_ids - wanted_ids

        if to_remove:
            await session.execute(
                delete(WatchedPair).where(
                    WatchedPair.asset_exchange_id.in_(to_remove)
                )
            )
            removed = len(to_remove)

    added = 0

    for spec_id in wanted_ids - existing_ids:
        session.add(WatchedPair(asset_exchange_id=spec_id))
        added += 1

    await session.commit()

    return added, removed


async def seed_watched_pairs(
    top=50, hours=24, jump_threshold=0.5, replace=False, bootstrap_mode=False,
    candidates=BOOTSTRAP_CANDIDATES, watch_minutes=BOOTSTRAP_WATCH_MINUTES,
):
    """Пересобирает watched_pair. Симулятор на это время останавливается.

    Иначе получается тихая порча данных: пара выпадает из watched_pair, цены
    по ней больше не приходят, а бот с открытой позицией продолжает видеть
    последнюю цену (у ключей price:* есть TTL, но 2 минуты он ещё живёт) и
    закрывается по ней. Плюс перезапуск питателя цен посреди сделки — это
    провал в котировках.
    """
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with paused_simulator(), dsm.get_session() as session:
        symbols = None

        if not bootstrap_mode:
            symbols = await rank_by_jumps(session, hours, top, jump_threshold)

            if not symbols:
                logging.info(
                    "В локальной истории нет ни одной пары со скачками — "
                    "перехожу на разгон с нуля."
                )

        if not symbols:
            # Разгон сам кладёт кандидатов в watched_pair и заменяет список,
            # поэтому итоговую запись делаем с replace в любом случае.
            symbols = await bootstrap(
                session=session, top=top, candidates_count=candidates,
                watch_minutes=watch_minutes, jump_threshold=jump_threshold,
            )
            replace = True

        if not symbols:
            logging.info("❌ Не удалось отобрать ни одной пары.")
            return

        # Список вышел вдвое короче запрошенного — значит что-то не сложилось:
        # сорвался добор (сеть), или рынок стоит. Удалять в этом случае нельзя:
        # питатель начнёт собирать историю только по остатку, и следующий отбор
        # замкнётся на нём же. Лучше оставить старые пары и разобраться.
        if replace and len(symbols) * 2 < top:
            logging.info(
                f"⚠️ Отобрано всего {len(symbols)} пар из {top} — только "
                f"пополняю список, не заменяю: иначе watched_pair схлопнется "
                f"до остатка. Разберитесь с причиной и запустите снова."
            )
            replace = False

        added, removed = await apply_watched_pairs(session, symbols, replace)

        total = (
            await session.execute(select(WatchedPair.asset_exchange_id))
        ).scalars().all()

        logging.info(
            f"✅ watched_pair: добавлено {added}, удалено {removed}, "
            f"всего пар в списке — {len(total)}."
        )

        if added or removed:
            # Без этого добранные пары остаются без котировок: в режиме
            # spot_ws подписка формируется один раз при подключении. Разгон
            # питатель уже перезапускал сам, но повторный перезапуск здесь
            # безвреден — симулятор всё равно ещё стоит.
            restart_price_feed()

        if not replace and removed == 0:
            logging.info(
                "Список только пополнялся. Чтобы оставить ровно топ, "
                "запустите с --replace."
            )


def main():
    parser = argparse.ArgumentParser(
        description="Наполняет watched_pair самыми «дёргаными» парами."
    )
    parser.add_argument('--top', type=int, default=50, help="Сколько пар взять.")
    parser.add_argument(
        '--hours', type=int, default=24,
        help="За какой период смотреть историю скачков."
    )
    parser.add_argument(
        '--jump-threshold', type=float, default=0.5,
        help="Какое движение за секунду считать скачком, в процентах."
    )
    parser.add_argument(
        '--replace', action='store_true',
        help="Заменить список, а не пополнить."
    )
    parser.add_argument(
        '--bootstrap', action='store_true',
        help="Разгон с нуля: кандидаты с Binance, слежка, отбор по скачкам."
    )
    parser.add_argument(
        '--candidates', type=int, default=BOOTSTRAP_CANDIDATES,
        help="Сколько кандидатов брать при разгоне."
    )
    parser.add_argument(
        '--watch-minutes', type=float, default=BOOTSTRAP_WATCH_MINUTES,
        help="Сколько минут следить за кандидатами при разгоне."
    )
    args = parser.parse_args()

    try:
        asyncio.run(seed_watched_pairs(
            top=args.top, hours=args.hours, jump_threshold=args.jump_threshold,
            replace=args.replace, bootstrap_mode=args.bootstrap,
            candidates=args.candidates, watch_minutes=args.watch_minutes,
        ))
    except SimulatorIsRunning as e:
        # Текст исключения — готовое сообщение человеку, трейсбек тут лишний.
        logging.error(str(e))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
