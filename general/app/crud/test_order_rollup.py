from datetime import datetime, timedelta

from sqlalchemy import func, select, text, union_all

from app.config import settings
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
