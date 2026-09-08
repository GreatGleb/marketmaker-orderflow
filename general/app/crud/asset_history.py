import asyncio
import logging
from decimal import Decimal

from sqlalchemy import delete, text, distinct
from sqlalchemy.dialects.postgresql import insert

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import aliased
from sqlalchemy import select, func

from app.config import settings
from app.constants.volatility import (
    JUMP_CAP_FACTOR,
    MAX_DROPPED_TICKS,
    MIN_JUMPS,
    MIN_QUOTE_VOLUME_24H,
    MIN_TICKS_IN_WINDOW,
    MIN_TICKS_SHARE_OF_MEDIAN,
    TICKS_PER_DROPPED_TICK,
    VOLATILITY_CANDIDATES,
)
from app.db.base import DatabaseSessionManager
from app.db.models import AssetHistory
from app.crud.base import BaseCrud


class AssetHistoryCrud(BaseCrud[AssetHistory]):
    def __init__(self, session):
        super().__init__(session, AssetHistory)
        logging.basicConfig(
            format='%(asctime)s - %(levelname)s - %(message)s',
            level=logging.INFO
        )

        logging.info('AssetHistoryCrud init')

    # asyncpg не принимает больше 32767 параметров в одном запросе,
    # поэтому большие пачки режем на куски по числу колонок.
    MAX_QUERY_ARGS = 30000

    async def bulk_create(self, items: list[dict]) -> None:
        if not items:
            return

        columns = max(len(item) for item in items)
        chunk_size = max(1, self.MAX_QUERY_ARGS // columns)

        for start in range(0, len(items), chunk_size):
            chunk = items[start:start + chunk_size]
            stmt = insert(AssetHistory).values(chunk)
            await self.session.execute(stmt)

    async def delete_older_than(self, cutoff_timestamp: datetime):
        stmt = delete(AssetHistory).where(
            AssetHistory.event_time < cutoff_timestamp
        )
        await self.session.execute(stmt)
        await self.session.commit()

    async def get_most_volatile_since(self, since: datetime, **kwargs):
        """Самая волатильная пара за окно или None, если ни одна не прошла отбор."""
        rows = await self.get_most_volatiles_since(
            since=since, limit=1, **kwargs
        )

        return rows[0] if rows else None

    async def get_top_jumpy_symbols(
        self, since: datetime, limit: int = 50, jump_threshold: float = 0.5,
        window_seconds: int = 1,
        min_quote_volume_24h: int = MIN_QUOTE_VOLUME_24H,
        min_jumps: int = MIN_JUMPS,
        jump_cap_factor: float = JUMP_CAP_FACTOR,
    ):
        """Пары с самыми резкими скачками цены, а не с самым большим разбросом.

        Скачок — движение больше jump_threshold процентов внутри окна в
        window_seconds секунд. Одно движение растянуто на много тиков, поэтому
        считаем только «передние фронты»: моменты, когда окно превысило порог,
        а предыдущее ещё нет. Иначе один рывок засчитался бы сотню раз.

        Ранжируем по сумме скачков — так учитываются и частота, и размер.

        Отбор по скачкам сам по себе не спасает ни от неликвида, ни от битых
        тиков: на августовской истории в топе нашлось 30 пар, из них 8 с
        оборотом ниже $2M, а 16 попали туда за один единственный «скачок».
        Поэтому:

        * пары с оборотом за сутки ниже min_quote_volume_24h не участвуют —
          фильтр стоит до оконных функций, так что заодно считается быстрее;
        * пара обязана показать не меньше min_jumps фронтов: битый тик даёт
          ровно один, и одним рывком в список теперь не попасть;
        * вклад одного скачка в сумму ограничен jump_cap_factor порогами,
          иначе одна аномалия перебивает десяток настоящих рывков.
        """
        query = text("""
            with
            -- Считаем по одному источнику. В asset_history лежат и фьючерсы
            -- (BINANCE), и спот (BINANCE_SPOT) — режим питателя цен
            -- переключается настройкой. Окно, попавшее на переключение,
            -- иначе смешивает два ряда цен, и сдвиг на базис между ними
            -- выглядит как мгновенное движение, которого не было.
            --
            -- Текущим считается источник с самыми свежими тиками в окне, а
            -- при равной свежести — тот, которым их больше: выбор обязан
            -- быть однозначным, иначе отбор начнёт скакать между режимами.
            current_source as (
                select source
                from asset_history
                where event_time >= :since
                group by source
                order by max(event_time) desc, count(*) desc
                limit 1
            ),
            liquid as (
                select symbol
                from asset_history
                where event_time >= :since
                  and last_price > 0
                  and source = (select source from current_source)
                group by symbol
                having max(quote_asset_volume_24h) >= :min_quote_volume_24h
            ),
            windowed as (
                select
                    h.symbol,
                    h.event_time,
                    (max(h.last_price) over w - min(h.last_price) over w)
                        / nullif(min(h.last_price) over w, 0) * 100 as jump_pct
                from asset_history h
                join liquid using (symbol)
                where h.event_time >= :since
                  and h.last_price > 0
                  and h.source = (select source from current_source)
                window w as (
                    partition by h.symbol order by h.event_time
                    range between :window_seconds preceding and current row
                )
            ),
            edges as (
                select
                    symbol,
                    jump_pct,
                    lag(jump_pct) over (
                        partition by symbol order by event_time
                    ) as prev_jump_pct
                from windowed
            )
            select
                symbol,
                count(*) as jumps,
                sum(least(
                    jump_pct, :jump_threshold * :jump_cap_factor
                )) as jumps_sum,
                max(jump_pct) as max_jump
            from edges
            where jump_pct > :jump_threshold
              and (prev_jump_pct is null or prev_jump_pct <= :jump_threshold)
            group by symbol
            having count(*) >= :min_jumps
            order by jumps_sum desc
            limit :limit
        """).bindparams(
            since=since,
            window_seconds=timedelta(seconds=window_seconds),
            jump_threshold=jump_threshold,
            min_quote_volume_24h=min_quote_volume_24h,
            min_jumps=min_jumps,
            jump_cap_factor=jump_cap_factor,
            limit=limit,
        )

        result = await self.session.execute(query)

        return result.all()

    async def get_most_volatiles_since(
        self,
        since: datetime,
        limit: int = 10,
        min_ticks: int = MIN_TICKS_IN_WINDOW,
        min_ticks_share: float = MIN_TICKS_SHARE_OF_MEDIAN,
        min_quote_volume_24h: int = MIN_QUOTE_VOLUME_24H,
        max_dropped_ticks: int = MAX_DROPPED_TICKS,
    ):
        """Самые волатильные пары за окно, без мусора и одиночных выбросов.

        Сырой размах (max-min)/min ранжирует не то, что нужно: у неликвидной
        монеты один случайный тик даёт размах больше, чем настоящее движение у
        ликвидной пары, и в победители попадает то, чем нельзя торговать.

        Поэтому сначала отсекаются пары, у которых торговать нечем:

        * оборот за сутки ниже min_quote_volume_24h;
        * тиков в окне меньше min_ticks или меньше min_ticks_share от медианы
          по парам этого же окна. Планка относительная, потому что абсолютная
          частота тиков зависит от режима питателя цен: на истории за август
          (6-12 тиков в минуту) любой абсолютный порог отсекал всех, а от
          относительного остаётся лучшее из доступного.

        Потом у выживших размах считается не по самым крайним тикам, а по
        следующим за ними: max_dropped_ticks крайних отпечатков с каждой
        стороны отбрасывается. Выброс от настоящего движения отличается не
        размером, а одиночностью — по одной сделке в ордер не войти, а на
        настоящем ходе тиков у края много. Поэтому потолка на движение здесь
        нет: пара, реально сходившая на 30%, так и получит свои 30%.

        Отсечка по проценту от медианы цены (коридор) и отсечка по
        процентилям для этого не годятся: первая режет как раз сильные
        реальные движения, вторая — короткие рывки, ради которых пара и
        выбирается.

        Крайние цены достаются точечно по каждому кандидату, а не
        сортировкой всего окна: на истории за август сортировка часового окна
        (1.5 млн тиков) стоит 9 с, а этот запрос — 0.6 с на часовом окне и
        0.2 с на пятиминутном. Ценой этого пары сначала отбираются по сырому
        размаху (VOLATILITY_CANDIDATES штук), и настоящий размах считается
        уже у них.
        """
        query = text("""
            with
            -- Считаем по одному источнику. В asset_history лежат и фьючерсы
            -- (BINANCE), и спот (BINANCE_SPOT) — режим питателя цен
            -- переключается настройкой. Окно, попавшее на переключение,
            -- иначе смешивает два ряда цен, и сдвиг на базис между ними
            -- выглядит как мгновенное движение, которого не было.
            --
            -- Текущим считается источник с самыми свежими тиками в окне, а
            -- при равной свежести — тот, которым их больше: выбор обязан
            -- быть однозначным, иначе отбор начнёт скакать между режимами.
            current_source as (
                select source
                from asset_history
                where event_time >= :since
                group by source
                order by max(event_time) desc, count(*) desc
                limit 1
            ),
            per_symbol as (
                select
                    symbol,
                    count(*) as ticks,
                    max(quote_asset_volume_24h) as quote_volume_24h,
                    -- Выбрасывать крайние тики можно только там, где их
                    -- много: иначе на коротком окне отсечка съест выборку.
                    least(
                        :max_dropped_ticks,
                        count(*) / :ticks_per_dropped_tick
                    )::int as dropped,
                    (max(last_price) - min(last_price))
                        / min(last_price) as raw_volatility
                from asset_history
                where event_time >= :since
                  and last_price > 0
                  and source = (select source from current_source)
                group by symbol
            ),
            candidates as (
                select *
                from per_symbol
                where ticks >= greatest(
                          :min_ticks,
                          (
                              select percentile_disc(0.5)
                                  within group (order by ticks)
                              from per_symbol
                          ) * :min_ticks_share
                      )
                  and quote_volume_24h >= :min_quote_volume_24h
                order by raw_volatility desc
                limit :candidates
            ),
            edges as (
                select
                    c.symbol,
                    c.ticks,
                    c.quote_volume_24h,
                    c.dropped,
                    (
                        select h.last_price
                        from asset_history h
                        where h.symbol = c.symbol
                          and h.event_time >= :since
                          and h.last_price > 0
                          and h.source = (select source from current_source)
                        order by h.last_price
                        offset c.dropped
                        limit 1
                    ) as low_price,
                    (
                        select h.last_price
                        from asset_history h
                        where h.symbol = c.symbol
                          and h.event_time >= :since
                          and h.last_price > 0
                          and h.source = (select source from current_source)
                        order by h.last_price desc
                        offset c.dropped
                        limit 1
                    ) as high_price
                from candidates c
            )
            select
                symbol,
                (high_price - low_price) / low_price as volatility,
                ticks,
                quote_volume_24h,
                dropped
            from edges
            order by volatility desc
            limit :limit
        """).bindparams(
            since=since,
            min_ticks=min_ticks,
            min_ticks_share=min_ticks_share,
            min_quote_volume_24h=min_quote_volume_24h,
            max_dropped_ticks=max_dropped_ticks,
            ticks_per_dropped_tick=TICKS_PER_DROPPED_TICK,
            candidates=max(VOLATILITY_CANDIDATES, limit),
            limit=limit,
        )

        result = await self.session.execute(query)

        return result.all()

    async def get_most_volatiles_since_from_symbols_list(self, since: datetime, symbols_list):
        query = (
            select(
                AssetHistory.symbol,
                func.abs(
                    (func.max(AssetHistory.last_price) - func.min(AssetHistory.last_price))
                    / func.min(AssetHistory.last_price)
                ).label("volatility")
            )
            .where(AssetHistory.event_time >= since)
            .where(AssetHistory.symbol.in_(symbols_list))
            .group_by(AssetHistory.symbol)
            .order_by(text("volatility DESC"))
            .limit(10)
        )

        result = await self.session.execute(query)
        return result.scalars().all()

    async def get_latest_price(self, symbol: str) -> Decimal | None:
        stmt = (
            select(AssetHistory.last_price)
            .where(AssetHistory.symbol == symbol)
            .order_by(AssetHistory.event_time.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_all_active_pairs(self, is_need_full_info=False, since=None, only_symbols_in_period=False, timeout=5.0):
        result = []

        try:
            UTC = timezone.utc
            now = datetime.now(UTC)

            if not since:
                five_minutes_ago = now - timedelta(minutes=5)

                since = five_minutes_ago

            if only_symbols_in_period:
                logging.info(f'Getting only unique symbols active since: {since}')
                query_to_execute = select(distinct(AssetHistory.symbol)).where(AssetHistory.event_time >= since)
            else:
                ah_new = aliased(AssetHistory)

                sub_query_new = (
                    select(
                        ah_new.symbol, func.max(ah_new.event_time).label("max_time")
                    )
                    .where(ah_new.event_time >= since)
                    .group_by(ah_new.symbol)
                    .subquery()
                )

                query_to_execute = (
                    select(AssetHistory.symbol, AssetHistory.last_price)
                    .join(
                        sub_query_new,
                        (AssetHistory.symbol == sub_query_new.c.symbol)
                        & (AssetHistory.event_time == sub_query_new.c.max_time),
                    )
                )

                if is_need_full_info:
                    query_to_execute = (
                        select(AssetHistory)
                        .join(
                            sub_query_new,
                            (AssetHistory.symbol == sub_query_new.c.symbol)
                            & (AssetHistory.event_time == sub_query_new.c.max_time),
                        )
                    )

            if query_to_execute is None:
                logging.error("No query was constructed. This should not happen.")
                return []

            dsm = DatabaseSessionManager.create(settings.DB_URL)
            async with dsm.get_session() as session:
                self.session = session

                result = await asyncio.wait_for(self.session.execute(query_to_execute), timeout=timeout)
                result = result.scalars().all()
        except asyncio.TimeoutError:
            logging.error(
                f"Database query timed out after 5 seconds for query type: {'only_symbols' if only_symbols_in_period else ('full_info' if is_need_full_info else 'symbol_price')}. Please check database performance or increase timeout.")
        except Exception as e:
            logging.error(f"An unexpected error occurred during database query: {e}", exc_info=True)

        return result
