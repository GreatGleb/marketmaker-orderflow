from datetime import datetime, timedelta

from sqlalchemy import func, select, text

from app.db.models import TestOrder, TestOrderRollup
from app.crud.base import BaseCrud
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
