from datetime import datetime, timedelta

from sqlalchemy import func, select, text, union_all

from app.config import settings
from app.constants.copybot import (
    DONOR_HISTORY_MINUTES,
    PROFITABILITY_CHECK_MINUTES,
)
from app.db.models import TestOrder, TestOrderRollup
from app.crud.base import BaseCrud
from app.crud.test_bot import active_bots_subquery
from app.enums.event_type import StopReasonEvent


class TestOrderRollupCrud(BaseCrud[TestOrderRollup]):
    """Свёртки `test_orders` по блокам фиксированной длины.

    Свёртка — это то, что остаётся от сырых сделок после чистки, поэтому
    считать её нужно ровно один раз и одинаково при любом числе повторов.
    Отсюда устройство `build_range`: сначала снести всё, что уже посчитано
    для этого промежутка, потом посчитать заново, и то и другое в одной
    транзакции.
    """

    # Один INSERT на блок при разборе накопившегося хвоста — это тысяча
    # запросов на неделю простоя. Блоки группируются по часу: `bucket_start`
    # всё равно в GROUP BY, поэтому один запрос сворачивает сразу шесть
    # десятиминутных блоков.
    CHUNK_MINUTES = 60

    def __init__(self, session):
        super().__init__(session, TestOrderRollup)

    async def last_bucket_start(self) -> datetime | None:
        """Начало последнего посчитанного блока."""
        result = await self.session.execute(
            select(func.max(TestOrderRollup.bucket_start))
        )
        return result.scalar()

    async def oldest_raw_order_at(self) -> datetime | None:
        """`created_at` самой старой сырой сделки.

        Дешёвый запрос: `ix_test_orders_created_at` отдаёт минимум с первой
        страницы индекса, таблицу читать не нужно.
        """
        result = await self.session.execute(
            select(func.min(TestOrder.created_at))
        )
        return result.scalar()

    async def build_range(
        self, start: datetime, end: datetime, bucket_minutes: int
    ) -> int:
        """Считает свёртки для `[start, end)`. Возвращает число строк.

        Причины закрытия считаются отдельными счётчиками, а не отдельными
        строками: значений всего три, и распределение сохраняется, не
        размножая свёртку втрое.

        Повторный вызов на том же промежутке перезаписывает уже посчитанное,
        а не добавляет второй комплект: ключ свёртки уникален. Это важно не
        только для ручного пересчёта — так два наложившихся прохода воркера
        не портят цифры.

        Границы обязаны попадать ровно на блоки. Иначе край запроса разрежет
        блок пополам: `date_bin` всё равно отнесёт обе половинки к одному
        `bucket_start`, вторая перезапишет первую по уникальному ключу — и
        часть сделок исчезнет из статистики. Хуже того, ретеншн считает
        границей безопасного удаления конец последнего свёрнутого блока и
        снесёт сырьё, которого в свёртке нет. Поэтому не выравниваем молча,
        а падаем.
        """
        bucket = timedelta(minutes=bucket_minutes)

        for name, edge in (("start", start), ("end", end)):
            if floor_to_bucket(edge, bucket) != edge:
                raise ValueError(
                    f"{name}={edge.isoformat()} не попадает на границу блока "
                    f"в {bucket_minutes} мин"
                )

        result = await self.session.execute(
            text(
                """
                INSERT INTO test_order_rollups (
                    bucket_start, bot_id, referral_bot_id, asset_symbol,
                    orders_count, profitable_count, profit_loss_sum, fee_sum,
                    stop_won_count, stop_loosed_count, stop_long_lose_count,
                    created_at, updated_at
                )
                SELECT
                    date_bin(
                        make_interval(mins => :bucket_minutes),
                        created_at,
                        TIMESTAMPTZ 'epoch'
                    ),
                    bot_id,
                    referral_bot_id,
                    asset_symbol,
                    count(*),
                    count(*) FILTER (WHERE profit_loss > 0),
                    sum(profit_loss),
                    sum(coalesce(open_fee, 0) + coalesce(close_fee, 0)),
                    count(*) FILTER (WHERE stop_reason_event = :won),
                    count(*) FILTER (WHERE stop_reason_event = :loosed),
                    count(*) FILTER (WHERE stop_reason_event = :long_lose),
                    now(),
                    now()
                FROM test_orders
                WHERE created_at >= :start AND created_at < :end
                GROUP BY 1, 2, 3, 4
                ON CONFLICT
                    (bucket_start, bot_id, referral_bot_id, asset_symbol)
                DO UPDATE SET
                    orders_count = EXCLUDED.orders_count,
                    profitable_count = EXCLUDED.profitable_count,
                    profit_loss_sum = EXCLUDED.profit_loss_sum,
                    fee_sum = EXCLUDED.fee_sum,
                    stop_won_count = EXCLUDED.stop_won_count,
                    stop_loosed_count = EXCLUDED.stop_loosed_count,
                    stop_long_lose_count = EXCLUDED.stop_long_lose_count,
                    updated_at = now()
                """
            ),
            {
                "start": start,
                "end": end,
                "bucket_minutes": bucket_minutes,
                "won": StopReasonEvent.STOP_WON.value,
                "loosed": StopReasonEvent.STOP_LOOSED.value,
                "long_lose": StopReasonEvent.STOP_LONG_LOSE.value,
            },
        )

        await self.session.commit()

        return result.rowcount

    # Имена колонок статистики. Обе ветки объединения обязаны отдавать их
    # в этом порядке и под этими именами: имена подзапроса UNION берутся из
    # первой ветки, и разъехавшийся порядок молча сложит комиссию с числом
    # сделок.
    STAT_COLUMNS = (
        "orders_count",
        "profitable_count",
        "profit_loss",
        "fee",
        "stop_won",
        "stop_loosed",
        "stop_long_lose",
    )

    async def rollup_watermark(self) -> datetime | None:
        """Момент, до которого статистика уже лежит в свёртках.

        Это конец последнего посчитанного блока — ровно та же граница, по
        которой ретеншн разрешает себе удалять сырьё (`retention.py`).
        Поэтому она же делит окно отчёта: до неё читаем свёртки, после —
        сырые сделки. `None` — свёрток нет вовсе.
        """
        last = await self.last_bucket_start()

        if last is None:
            return None

        return last + timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES)

    async def earliest_data_at(self) -> datetime | None:
        """Самый ранний момент, за который вообще осталась статистика.

        Нужен отчёту, чтобы не врать заголовком: если попросили две недели,
        а свёртки живут неделю, окно надо показать настоящее.

        `LEAST` в Postgres пропускает NULL, поэтому пустая таблица из двух
        не мешает второй ответить.
        """
        result = await self.session.execute(
            select(
                func.least(
                    select(
                        func.min(TestOrderRollup.bucket_start)
                    ).scalar_subquery(),
                    select(func.min(TestOrder.created_at)).scalar_subquery(),
                )
            )
        )

        return result.scalar()

    async def profit_by_bot(
        self,
        since: datetime,
        just_copy_bots=False,
        just_copy_bots_v2=False,
        just_not_copy_bots=False,
        symbol=None,
        by_referral_bot_id=False,
    ) -> tuple[list, datetime, datetime | None]:
        """Статистика по каждому боту за окно `[since, сейчас)`.

        В отличие от `TestBotCrud.get_sorted_by_profit`, которая читает
        только `test_orders`, эта считает по обоим источникам сразу:
        свёртки за всё, что уже свёрнуто, сырые сделки за хвост после
        границы свёрнутого. Иначе окно длиннее
        `RETENTION_TEST_ORDERS_HOURS` молча обрезалось бы до срока хранения
        сырья — без ошибки, просто с неправильными цифрами.

        Двойного счёта не будет: границы веток стыкуются по watermark, а не
        перекрываются. Сырьё за уже свёрнутые блоки в базе какое-то время
        лежит (ретеншн ходит раз в час) — из отчёта оно исключено.

        Возвращает `(строки, начало окна, граница свёрнутого)`. Начало окна
        возвращается посчитанным, а не запрошенным: оно округляется вниз до
        блока, потому что мельче блока свёртки ничего не знают.
        """
        bucket = timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES)
        watermark = await self.rollup_watermark()

        rollup_from, rollup_to, raw_from = split_window(
            since=since, watermark=watermark, bucket=bucket
        )

        bots = active_bots_subquery(
            just_copy_bots=just_copy_bots,
            just_copy_bots_v2=just_copy_bots_v2,
            just_not_copy_bots=just_not_copy_bots,
            symbol=symbol,
        )

        parts = []

        if rollup_from is not None:
            parts.append(
                self.rollup_part(bots, rollup_from, rollup_to, by_referral_bot_id)
            )

        parts.append(self.raw_part(bots, raw_from, by_referral_bot_id))

        combined = (
            parts[0] if len(parts) == 1 else union_all(*parts)
        ).subquery()

        stmt = (
            select(
                combined.c.bot_id,
                *[
                    func.sum(combined.c[name]).label(name)
                    for name in self.STAT_COLUMNS
                ],
            )
            .group_by(combined.c.bot_id)
            .order_by(func.sum(combined.c.profit_loss).desc())
        )

        rows = (await self.session.execute(stmt)).all()

        return rows, rollup_from or raw_from, watermark

    @classmethod
    def rollup_part(cls, bots, start: datetime, end: datetime, by_referral):
        """Ветка по свёрткам: складываем уже посчитанное."""
        key = (
            TestOrderRollup.referral_bot_id
            if by_referral
            else TestOrderRollup.bot_id
        )

        return (
            select(
                key.label("bot_id"),
                func.sum(TestOrderRollup.orders_count).label("orders_count"),
                func.sum(TestOrderRollup.profitable_count).label(
                    "profitable_count"
                ),
                func.sum(
                    func.coalesce(TestOrderRollup.profit_loss_sum, 0)
                ).label("profit_loss"),
                func.sum(func.coalesce(TestOrderRollup.fee_sum, 0)).label(
                    "fee"
                ),
                func.sum(TestOrderRollup.stop_won_count).label("stop_won"),
                func.sum(TestOrderRollup.stop_loosed_count).label(
                    "stop_loosed"
                ),
                func.sum(TestOrderRollup.stop_long_lose_count).label(
                    "stop_long_lose"
                ),
            )
            .where(
                key.in_(bots),
                TestOrderRollup.bucket_start >= start,
                TestOrderRollup.bucket_start < end,
            )
            .group_by(key)
        )

    @classmethod
    def raw_part(cls, bots, start: datetime, by_referral):
        """Ветка по сырым сделкам: считаем ровно то же, что `build_range`.

        Если эти два счёта разойдутся, отчёт даст разрыв ровно на границе
        свёрнутого — самое неприятное расхождение из возможных, потому что
        граница каждый час уезжает.
        """
        key = (
            TestOrder.referral_bot_id if by_referral else TestOrder.bot_id
        )

        return (
            select(
                key.label("bot_id"),
                func.count().label("orders_count"),
                func.count()
                .filter(TestOrder.profit_loss > 0)
                .label("profitable_count"),
                func.sum(func.coalesce(TestOrder.profit_loss, 0)).label(
                    "profit_loss"
                ),
                func.sum(
                    func.coalesce(TestOrder.open_fee, 0)
                    + func.coalesce(TestOrder.close_fee, 0)
                ).label("fee"),
                func.count()
                .filter(
                    TestOrder.stop_reason_event
                    == StopReasonEvent.STOP_WON.value
                )
                .label("stop_won"),
                func.count()
                .filter(
                    TestOrder.stop_reason_event
                    == StopReasonEvent.STOP_LOOSED.value
                )
                .label("stop_loosed"),
                func.count()
                .filter(
                    TestOrder.stop_reason_event
                    == StopReasonEvent.STOP_LONG_LOSE.value
                )
                .label("stop_long_lose"),
            )
            .where(key.in_(bots), TestOrder.created_at >= start)
            .group_by(key)
        )

    DONOR_MATCH_SQL = """
        with
        -- Кандидаты в доноры — те же, кого перебирает воркер:
        -- profitable_bot_ids_for_window ходит с just_not_copy_bots=True.
        non_copy as (
            select id from test_bots
            where is_active
              and copy_bot_min_time_profitability_min is null
              and copybot_v2_time_in_minutes is null
        ),
        -- Что копиботы этой комбинации параметров делали на самом деле.
        -- Строка свёртки — на (блок, бот, донор, пара), донор нас интересует
        -- целиком по паре, поэтому пары схлопываем.
        used as (
            select
                r.bucket_start,
                r.bot_id,
                r.referral_bot_id,
                sum(r.orders_count) as orders_count,
                sum(r.profit_loss_sum) as profit_loss
            from test_order_rollups r
            join test_bots b on b.id = r.bot_id
            where b.is_active
              and b.copy_bot_min_time_profitability_min = :tf
              and b.copybot_v1_check_for_24h_profitability = :check_24h
              and b.copybot_v1_exclude_losing_donors = :exclude_losing
              and r.referral_bot_id is not null
              and r.bucket_start >= :since
              and r.bucket_start < :until
            group by 1, 2, 3
        ),
        -- Считаем не на каждый блок подряд, а только там, где копиботы этой
        -- комбинации торговали: в тихие блоки заглядывать незачем.
        points as (select distinct bucket_start from used),
        candidates as (
            select
                p.bucket_start,
                e.bot_id,
                e.pnl_tf,
                e.pnl_long,
                d.pnl_as_donor
            from points p
            cross join lateral (
                -- Оба окна одним проходом: длинное задаёт границу выборки,
                -- короткое вырезается из неё через filter. Второй скан по
                -- тем же строкам ради 24 часов не нужен.
                select
                    r.bot_id,
                    sum(r.profit_loss_sum) filter (
                        where r.bucket_start
                              >= p.bucket_start - make_interval(mins => (:tf)::int)
                    ) as pnl_tf,
                    sum(r.profit_loss_sum) as pnl_long
                from test_order_rollups r
                join non_copy nc on nc.id = r.bot_id
                where r.bucket_start
                      >= p.bucket_start - make_interval(mins => (:long_minutes)::int)
                  and r.bucket_start < p.bucket_start
                group by r.bot_id
            ) e
            left join lateral (
                -- Чем кончилось копирование этого бота у других: столько
                -- заработали те, кто за ним копировал. Окно фиксированное и
                -- не равно окну копибота — плохой перенос конфига свойство
                -- устойчивое, за десять минут его не увидеть.
                select sum(r.profit_loss_sum) as pnl_as_donor
                from test_order_rollups r
                where r.referral_bot_id = e.bot_id
                  and r.bucket_start
                      >= p.bucket_start
                         - make_interval(mins => (:donor_minutes)::int)
                  and r.bucket_start < p.bucket_start
            ) d on true
        ),
        -- Те же три условия, что у filter_profitable_bots_id, и тот же
        -- порядок: по сумме P/L за окно, от большего к меньшему.
        eligible as (
            select
                bucket_start,
                bot_id,
                pnl_tf,
                rank() over (
                    partition by bucket_start order by pnl_tf desc
                ) as rnk
            from candidates
            where pnl_tf > 0
              and (:check_24h = false or coalesce(pnl_long, 0) > 0)
              -- `>= 0`, а не `> 0`: фильтр выбрасывает донора, у которого
              -- копиры ушли в минус, но не требует, чтобы копиры вообще
              -- были. NULL при отсутствии истории даёт 0 и проходит.
              and (:exclude_losing = false
                   or coalesce(pnl_as_donor, 0) >= 0)
        )
        select
            u.bucket_start,
            u.bot_id as copy_bot_id,
            u.referral_bot_id,
            u.orders_count,
            u.profit_loss,
            e.rnk as donor_rank,
            top.bot_id as expected_bot_id,
            (
                select count(*) from eligible x
                where x.bucket_start = u.bucket_start
            ) as eligible_count
        from used u
        left join eligible e
            on e.bucket_start = u.bucket_start
           and e.bot_id = u.referral_bot_id
        left join lateral (
            select x.bot_id from eligible x
            where x.bucket_start = u.bucket_start
            order by x.rnk
            limit 1
        ) top on true
        order by u.bucket_start, u.bot_id
    """

    async def donor_match(
        self, tf, check_24h: bool, exclude_losing: bool,
        since: datetime, until: datetime,
    ):
        """Кого копибот взял в доноры против того, кого должен был.

        Отвечает на вопрос, ради которого когда-то заводили колонку
        `test_orders.referral_bot_from_profit_func`: сходится ли донор,
        прочитанный из Redis, с тем, кого даёт функция отбора. Только
        постфактум и по свёрткам, а не пересчётом на каждое открытие сделки —
        последнее и было причиной, по которой ту проверку выключили через
        четыре дня после появления (08-gotchas.md, пункт 10b).

        Отбор воспроизводится ровно тот же, что у воркера
        (`ProfitableBotUpdaterCommand.filter_profitable_bots_id`): среди
        некопиботов берутся прибыльные за окно бота, при `check_24h` — ещё и
        прибыльные за сутки, при `exclude_losing` — из них выбрасываются те,
        у кого копиры за сутки ушли в минус; победитель первый по сумме P/L.
        Расходиться этим двум определениям нельзя: разойдутся — отчёт начнёт
        мерить сам себя.

        Возвращает строку на (блок, копибот, донор) с местом взятого донора в
        этом рейтинге. `donor_rank = 1` — попали точно, `None` — взятый донор
        в тот момент вообще не проходил отбор.

        Две неточности, обе известные и обе меньше блока:

        * блок — десять минут, а воркер считает от точного `now()`. Место
          донора поэтому даётся на начало блока, а не на секунду открытия
          сделки; окна копиботов кратны десяти минутам, так что грубее самого
          отбора отчёт не становится;
        * свёртки бьются по `created_at`, то есть по закрытию сделки, а
          донора выбирают на открытии. Сделки живут до 30 секунд, внутри
          блока это не видно.
        """
        rows = await self.session.execute(
            text(self.DONOR_MATCH_SQL),
            {
                "tf": int(tf),
                "check_24h": bool(check_24h),
                "exclude_losing": bool(exclude_losing),
                # При выключенном флаге сутки не нужны: короткое окно тогда
                # и задаёт границу выборки.
                "long_minutes": (
                    max(int(tf), PROFITABILITY_CHECK_MINUTES)
                    if check_24h else int(tf)
                ),
                "donor_minutes": DONOR_HISTORY_MINUTES,
                "since": since,
                "until": until,
            },
        )

        return rows.all()

    async def delete_older_than(self, cutoff: datetime) -> int:
        """Чистка самих свёрток.

        Строка свёртки в сотни раз мельче блока сырых сделок, который она
        заменяет, поэтому хранить их можно годами — но не вечно.
        """
        result = await self.session.execute(
            text(
                "DELETE FROM test_order_rollups WHERE bucket_start < :cutoff"
            ),
            {"cutoff": cutoff},
        )
        await self.session.commit()

        return result.rowcount


def floor_to_bucket(moment: datetime, bucket: timedelta) -> datetime:
    """Начало блока, в который попадает `moment`.

    Считается от эпохи — тем же способом, что `date_bin` внутри базы, иначе
    границы блоков в Python и в SQL разъедутся.
    """
    seconds = moment.timestamp()
    step = bucket.total_seconds()

    return datetime.fromtimestamp(
        seconds - (seconds % step), tz=moment.tzinfo
    )


def split_window(
    since: datetime, watermark: datetime | None, bucket: timedelta
) -> tuple[datetime | None, datetime | None, datetime]:
    """Как поделить окно отчёта между свёртками и сырьём.

    Возвращает `(откуда свёртки, докуда свёртки, откуда сырьё)`; первые два
    — `None`, если свёртки в этом окне не нужны.

    Правило одно: ветки стыкуются по watermark и не перекрываются. Сдвинь
    любую границу на блок — и сделки этого блока либо посчитаются дважды,
    либо не посчитаются вовсе.

    Начало округляется вниз до блока: свёртка не знает, что было внутри
    десяти минут, поэтому взять её половину нельзя. Округление вниз делает
    окно чуть шире запрошенного, а не уже — недосчитать хуже, чем
    прихватить лишние минуты, и отчёт всё равно печатает настоящую границу.
    """
    since = floor_to_bucket(since, bucket)

    if watermark is None or watermark <= since:
        # Свёрток нет, или всё окно лежит уже после границы свёрнутого —
        # читаем только сырьё.
        return None, None, since

    return since, watermark, watermark
