"""Ретеншн целиком, на настоящей базе.

Проверять свёртки и чистку на заглушках бессмысленно: вся суть в SQL —
`date_bin` должен резать блоки так же, как Python, уникальный ключ должен
ловить повтор, а удаление пачками по `ctid` должно удалять ровно то, что
просили. Поэтому тест поднимает схему в отдельной базе и гоняет настоящие
воркеры.

База нужна отдельная: чистка удаляет по времени, а не по меткам теста, и на
рабочей базе снесла бы настоящие сделки. Поэтому адрес передаётся явно.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists retention_check' \\
        -c 'create database retention_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/retention_check \\
      -e RETENTION_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_retention_db

Схема накатывается `alembic upgrade head` — тест её не создаёт.

Без `RETENTION_TEST_DB` проверка пропускается: остальные тесты в каталоге
идут на заглушках и не должны требовать поднятого стека.
"""
import asyncio
import os
import sys

from datetime import datetime, timedelta, UTC
from decimal import Decimal

from sqlalchemy import text
from unittest.mock import patch

from app.config import settings
from app.crud.test_order_rollup import TestOrderRollupCrud, floor_to_bucket
from app.db.base import DatabaseSessionManager
from app.enums.event_type import StopReasonEvent
from app.workers.retention import RetentionCommand
from app.workers.test_order_rollup import TestOrderRollupCommand

BUCKET = timedelta(minutes=10)

# Сколько сделок засеять и как их разложить. Три бота, две пары, все три
# причины закрытия, у части сделок пустой referral_bot_id — именно на нём
# ломается уникальный ключ без NULLS NOT DISTINCT.
BOTS = [11, 12, 13]
SYMBOLS = ["BTCUSDT", "ETHUSDT"]
REASONS = [
    StopReasonEvent.STOP_WON.value,
    StopReasonEvent.STOP_LOOSED.value,
    StopReasonEvent.STOP_LONG_LOSE.value,
]


async def seed(session, now, hours_back):
    """Кладёт по сделке на каждые полминуты за последние `hours_back` часов."""
    await session.execute(text("TRUNCATE test_orders, test_order_rollups"))

    # bot_id и referral_bot_id — внешние ключи на test_bots, без ботов
    # сделки не вставятся.
    await session.execute(
        text(
            """
            INSERT INTO test_bots (id, symbol, balance, is_active)
            SELECT unnest(CAST(:ids AS integer[])), 'BTCUSDT', 100, false
            ON CONFLICT (id) DO NOTHING
            """
        ),
        {"ids": BOTS + [bot + 100 for bot in BOTS]},
    )

    rows = []
    moment = now - timedelta(hours=hours_back)
    index = 0

    while moment < now:
        bot_id = BOTS[index % len(BOTS)]
        rows.append(
            {
                "asset_symbol": SYMBOLS[index % len(SYMBOLS)],
                "balance": 100.0,
                "order_type": "long",
                "open_price": 100.0,
                "open_time": moment,
                "open_fee": 0.02,
                "stop_loss_price": 99.0,
                "close_price": 101.0,
                "close_time": moment,
                "close_fee": 0.02,
                # Каждая третья сделка убыточна — так проверяется
                # profitable_count, а не просто «всё сошлось на нулях».
                "profit_loss": Decimal("-1.5")
                if index % 3 == 2
                else Decimal("0.5"),
                "is_active": False,
                "bot_id": bot_id,
                # У некопиботов реферального нет — это NULL в ключе свёртки.
                "referral_bot_id": None if bot_id == 11 else bot_id + 100,
                "stop_reason_event": REASONS[index % len(REASONS)],
                "created_at": moment,
                "updated_at": moment,
            }
        )
        moment += timedelta(seconds=30)
        index += 1

    for start in range(0, len(rows), 500):
        await session.execute(
            text(
                """
                INSERT INTO test_orders (
                    asset_symbol, balance, order_type, open_price, open_time,
                    open_fee, stop_loss_price, close_price, close_time,
                    close_fee, profit_loss, is_active, bot_id,
                    referral_bot_id, stop_reason_event, created_at, updated_at
                ) VALUES (
                    :asset_symbol, :balance, :order_type, :open_price,
                    :open_time, :open_fee, :stop_loss_price, :close_price,
                    :close_time, :close_fee, :profit_loss, :is_active,
                    :bot_id, :referral_bot_id, :stop_reason_event,
                    :created_at, :updated_at
                )
                """
            ),
            rows[start:start + 500],
        )

    await session.commit()

    return len(rows)


async def scalar(session, sql, **params):
    return (await session.execute(text(sql), params)).scalar()


async def check_rollups_match_raw(session, edge):
    """Свёртка обязана давать те же цифры, что и сырые сделки.

    Сравнивается только свёрнутое: последний час ещё открыт, свёрток за него
    нет и быть не должно.
    """
    mismatch = await scalar(
        session,
        """
        WITH raw AS (
            SELECT date_bin(interval '10 minutes', created_at,
                            TIMESTAMPTZ 'epoch') AS bucket,
                   bot_id, referral_bot_id, asset_symbol,
                   count(*) AS orders,
                   count(*) FILTER (WHERE profit_loss > 0) AS good,
                   sum(profit_loss) AS pnl
            FROM test_orders
            WHERE created_at < :edge
            GROUP BY 1, 2, 3, 4
        )
        SELECT count(*)
        FROM raw
        FULL JOIN test_order_rollups AS r
          ON r.bucket_start = raw.bucket
         AND r.bot_id IS NOT DISTINCT FROM raw.bot_id
         AND r.referral_bot_id IS NOT DISTINCT FROM raw.referral_bot_id
         AND r.asset_symbol = raw.asset_symbol
        WHERE (raw.bucket IS NULL AND r.bucket_start < :edge)
           OR raw.bucket IS NOT NULL AND (
                r.bucket_start IS NULL
                OR r.orders_count <> raw.orders
                OR r.profitable_count <> raw.good
                OR r.profit_loss_sum <> raw.pnl
           )
        """,
        edge=edge,
    )

    assert mismatch == 0, f"свёртки разошлись с сырыми данными: {mismatch}"


async def run_checks(session, rollup_crud):
    now = datetime.now(UTC)

    print("  засеваю 8 часов сделок...")
    seeded = await seed(session, now, hours_back=8)
    print(f"    сделок в базе: {seeded}")

    # Отставание в час: свернётся всё, кроме последнего часа.
    with patch.object(settings, "ROLLUP_LAG_MINUTES", 60):
        await TestOrderRollupCommand().command(rollup_crud=rollup_crud)

    rollup_rows = await scalar(
        session, "SELECT count(*) FROM test_order_rollups"
    )
    watermark = await rollup_crud.last_bucket_start()
    assert rollup_rows > 0, "свёртки не посчитались"
    assert watermark < now - timedelta(minutes=55), watermark

    covered = await scalar(
        session,
        "SELECT sum(orders_count) FROM test_order_rollups",
    )
    raw_covered = await scalar(
        session,
        "SELECT count(*) FROM test_orders WHERE created_at < :edge",
        edge=watermark + BUCKET,
    )
    assert covered == raw_covered, f"{covered} против {raw_covered}"
    print(f"    строк свёрток: {rollup_rows}, в них сделок: {covered}")

    print("  повторный проход не должен ничего задвоить...")
    before = rollup_rows
    with patch.object(settings, "ROLLUP_LAG_MINUTES", 60):
        await TestOrderRollupCommand().command(rollup_crud=rollup_crud)

    await rollup_crud.build_range(
        start=watermark - timedelta(hours=2), end=watermark, bucket_minutes=10
    )
    after = await scalar(session, "SELECT count(*) FROM test_order_rollups")
    assert after == before, f"было {before}, стало {after}"
    await check_rollups_match_raw(session, edge=watermark + BUCKET)
    print(f"    строк свёрток по-прежнему {after}, цифры сходятся с сырыми")

    print("  чистка с окном в 6 часов...")
    with patch.object(settings, "RETENTION_TEST_ORDERS_HOURS", 6):
        with patch.object(settings, "RETENTION_BATCH_ROWS", 100):
            result = await RetentionCommand().run_async()

    assert result.success, result

    oldest = await scalar(session, "SELECT min(created_at) FROM test_orders")
    left = await scalar(session, "SELECT count(*) FROM test_orders")

    assert oldest >= now - timedelta(hours=6, minutes=1), oldest
    assert left < seeded, "чистка не удалила ничего"
    print(
        f"    осталось сделок: {left} из {seeded}, "
        f"самая старая: {oldest.isoformat()}"
    )

    # Свёртки за удалённое время обязаны остаться — ради них всё и затевалось.
    await check_survived_statistics(session, now)

    print("  чистка укладывается в отведённое время...")
    await check_time_budget_is_respected(session, rollup_crud, now)

    print("  границы блоков...")
    await check_partial_bucket_is_refused(rollup_crud, now)

    print("  чистка не забегает вперёд свёрток...")
    await check_stops_at_watermark(session, rollup_crud, now, seeded)


async def check_survived_statistics(session, now):
    survived = await scalar(
        session,
        """
        SELECT sum(orders_count) FROM test_order_rollups
        WHERE bucket_start < :edge
        """,
        edge=now - timedelta(hours=6),
    )
    raw_left = await scalar(
        session,
        "SELECT count(*) FROM test_orders WHERE created_at < :edge",
        edge=now - timedelta(hours=6),
    )

    assert survived > 0, "статистика за удалённое время пропала"
    assert raw_left == 0, f"сырое старше окна осталось: {raw_left}"
    print(f"    статистика по {survived} удалённым сделкам сохранена")


async def check_stops_at_watermark(session, rollup_crud, now, seeded):
    """Если свёртки отстали, чистка обязана остановиться на их границе.

    Ровно этим ретеншн и отличается от «удалить всё старше N часов»: пока
    статистика за блок не посчитана, сырьё за ним трогать нельзя, сколько бы
    ему ни было часов.
    """
    await seed(session, now, hours_back=8)

    # Свёртки отстают на шесть часов: воркер закроет только первые два часа
    # из восьми. Отставание задаётся через настройку, а не вызовом на
    # произвольном промежутке, — так границы блоков считает сам воркер.
    with patch.object(settings, "ROLLUP_LAG_MINUTES", 6 * 60):
        await TestOrderRollupCommand().command(rollup_crud=rollup_crud)

    watermark = await rollup_crud.last_bucket_start() + BUCKET

    # По окну хранения снести полагается всё старше часа — то есть намного
    # больше, чем свёрнуто.
    with patch.object(settings, "RETENTION_TEST_ORDERS_HOURS", 1):
        with patch.object(settings, "RETENTION_BATCH_ROWS", 100):
            await RetentionCommand().run_async()

    oldest = await scalar(session, "SELECT min(created_at) FROM test_orders")

    assert oldest >= watermark, (
        f"удалено раньше границы свёрнутого: {oldest} < {watermark}"
    )
    assert oldest < watermark + BUCKET, (
        f"чистка не дошла до границы свёрнутого: {oldest} vs {watermark}"
    )
    print(
        f"    остановилась на {oldest.isoformat()}, "
        f"граница свёрнутого {watermark.isoformat()}"
    )


async def check_time_budget_is_respected(session, rollup_crud, now):
    """Проход обязан остановиться по времени, а не домалывать любой ценой.

    Первый запуск после долгого простоя — это десятки миллионов строк.
    Удалить их за раз нельзя: база нужна ботам. Проход берёт сколько успел,
    честно говорит, что не добил, и возвращается через час.
    """
    await seed(session, now, hours_back=8)
    await TestOrderRollupCommand().command(rollup_crud=rollup_crud)

    before = await scalar(session, "SELECT count(*) FROM test_orders")

    # Бюджет на всю чистку — доли секунды, пачки по 10 строк.
    with patch.object(settings, "RETENTION_TEST_ORDERS_HOURS", 1):
        with patch.object(settings, "RETENTION_BATCH_ROWS", 10):
            with patch.object(settings, "RETENTION_MAX_SECONDS", 0):
                result = await RetentionCommand().run_async()

    after = await scalar(session, "SELECT count(*) FROM test_orders")

    assert result.success, result
    assert after < before, "проход не удалил вообще ничего"
    assert after > 0, "проход проигнорировал бюджет и добил всё"
    assert not result.data["finished"]["test_orders"], (
        "проход не сознался, что не добил хвост"
    )
    print(f"    удалено {before - after} из {before}, хвост честно оставлен")

    # Хвост добирается следующим проходом с обычным бюджетом — данные не
    # застревают из-за того, что один раз не хватило времени.
    with patch.object(settings, "RETENTION_TEST_ORDERS_HOURS", 1):
        with patch.object(settings, "RETENTION_BATCH_ROWS", 10):
            result = await RetentionCommand().run_async()

    assert result.data["finished"]["test_orders"], "хвост так и не добрался"
    left = await scalar(session, "SELECT count(*) FROM test_orders")
    print(f"    следующий проход добрал хвост, осталось {left}")


async def check_partial_bucket_is_refused(rollup_crud, now):
    """Свернуть половину блока нельзя — это молчаливая потеря сделок."""
    aligned = floor_to_bucket(now, BUCKET)

    try:
        await rollup_crud.build_range(
            start=aligned - timedelta(hours=1),
            end=aligned - timedelta(minutes=3),
            bucket_minutes=10,
        )
    except ValueError as error:
        print(f"    несовпадение с границей блока отбито: {error}")
        return

    raise AssertionError("свёртка приняла границу посреди блока")


async def main():
    if not os.getenv("RETENTION_TEST_DB"):
        print(
            "test_retention_db пропущен: нужна отдельная база.\n"
            "Как её поднять — в шапке файла."
        )
        return

    print(f"Ретеншн на живой базе ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        rollup_crud = TestOrderRollupCrud(session)
        await run_checks(session, rollup_crud)

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
