"""Измерения стратегии в свёртках, на настоящей базе.

Проверять это на заглушках нечем: всё поведение — в уникальном ключе и
группировке INSERT ... ON CONFLICT. Ломается оно молча: строки не падают,
а схлопываются, и часть сделок исчезает вместе с сырьём, которое удалит
ретеншн.

База нужна отдельная: тест чистит `test_orders` и `test_order_rollups`.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists strategy_check' \\
        -c 'create database strategy_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/strategy_check \\
      -e STRATEGY_ROLLUP_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_strategy_rollup_db

Схему накатывает `alembic upgrade head` — тест её не создаёт.

Без `STRATEGY_ROLLUP_TEST_DB` проверка пропускается.
"""
import asyncio
import os

from datetime import datetime, timedelta, UTC
from decimal import Decimal

from sqlalchemy import text

from app.config import settings
from app.constants.strategy import (
    LEGACY_ALGORITHM_VERSION,
    STRATEGY_LEGACY,
)
from app.crud.strategy import StrategyCrud
from app.crud.test_orders import TestOrderCrud
from app.crud.test_order_rollup import TestOrderRollupCrud, floor_to_bucket
from app.db.base import DatabaseSessionManager
from app.enums.event_type import StopReasonEvent

BUCKET_MINUTES = 10
BUCKET = timedelta(minutes=BUCKET_MINUTES)

COPYBOT_ID = 21
DONOR_LEGACY_ID = 22
DONOR_OTHER_ID = 23
SYMBOL = "BTCUSDT"

# Версия, с которой сделки записаны после правки алгоритма. Вторая версия
# внутри того же блока — не выдумка: правку выкатывают в середине дня.
NEXT_VERSION = "legacy-2"


async def prepare_bots(session, legacy_id, other_id):
    await session.execute(text("TRUNCATE test_orders, test_order_rollups"))
    await session.execute(
        text(
            """
            INSERT INTO test_bots (id, symbol, balance, is_active, strategy_id)
            VALUES
                (:copybot, '', 1000, true, :legacy),
                (:donor_legacy, :symbol, 1000, true, :legacy),
                (:donor_other, :symbol, 1000, true, :other)
            ON CONFLICT (id) DO UPDATE SET
                strategy_id = EXCLUDED.strategy_id,
                -- Боты от прошлого прогона остаются в базе: отчёт смотрит
                -- только на активных, и без этого повтор теста молча
                -- считал бы ноль сделок.
                is_active = EXCLUDED.is_active
            """
        ),
        {
            "copybot": COPYBOT_ID,
            "donor_legacy": DONOR_LEGACY_ID,
            "donor_other": DONOR_OTHER_ID,
            "symbol": SYMBOL,
            "legacy": legacy_id,
            "other": other_id,
        },
    )
    await session.commit()


async def insert_order(session, moment, *, referral, strategy, executed,
                       version, profit):
    await session.execute(
        text(
            """
            INSERT INTO test_orders (
                asset_symbol, balance, order_type, open_price, open_time,
                open_fee, stop_loss_price, close_price, close_time,
                close_fee, profit_loss, is_active, bot_id, referral_bot_id,
                stop_reason_event, strategy_id, executed_strategy_id,
                algorithm_version, created_at, updated_at
            ) VALUES (
                :symbol, 1000, 'long', 100, :moment, 0.02, 99, 101, :moment,
                0.02, :profit, false, :bot_id, :referral, :reason,
                :strategy, :executed, :version, :moment, :moment
            )
            """
        ),
        {
            "symbol": SYMBOL,
            "moment": moment,
            "profit": profit,
            "bot_id": COPYBOT_ID,
            "referral": referral,
            "reason": StopReasonEvent.STOP_WON.value,
            "strategy": strategy,
            "executed": executed,
            "version": version,
        },
    )


async def rollup_rows(session, bucket_start):
    result = await session.execute(
        text(
            """
            SELECT executed_strategy_id, algorithm_version, referral_bot_id,
                   orders_count, profit_loss_sum
            FROM test_order_rollups
            WHERE bucket_start = :bucket_start
            ORDER BY executed_strategy_id, algorithm_version,
                     referral_bot_id
            """
        ),
        {"bucket_start": bucket_start},
    )
    return result.all()


async def check_dimensions(session, rollup_crud, legacy_id, other_id):
    """Сделки одного бота в одном блоке не схлопываются по стратегиям."""
    now = datetime.now(UTC)
    bucket_start = floor_to_bucket(now - BUCKET, BUCKET)
    moment = bucket_start + timedelta(minutes=1)

    # Один и тот же копибот за один блок: сначала донор своей стратегии,
    # потом донор чужой, потом снова своей, но уже после правки алгоритма.
    await insert_order(
        session, moment, referral=DONOR_LEGACY_ID, strategy=legacy_id,
        executed=legacy_id, version=LEGACY_ALGORITHM_VERSION,
        profit=Decimal("1.5"),
    )
    await insert_order(
        session, moment + timedelta(minutes=1), referral=DONOR_OTHER_ID,
        strategy=legacy_id, executed=other_id,
        version=LEGACY_ALGORITHM_VERSION, profit=Decimal("-0.5"),
    )
    await insert_order(
        session, moment + timedelta(minutes=2), referral=DONOR_LEGACY_ID,
        strategy=legacy_id, executed=legacy_id, version=NEXT_VERSION,
        profit=Decimal("2.0"),
    )
    await session.commit()

    await rollup_crud.build_range(
        bucket_start, bucket_start + BUCKET, BUCKET_MINUTES
    )

    rows = await rollup_rows(session, bucket_start)

    assert len(rows) == 3, (
        f"ожидались три строки (своя стратегия, чужая, новая версия), "
        f"получено {len(rows)}: {rows}"
    )

    by_key = {
        (executed, version): (count, total)
        for executed, version, _, count, total in rows
    }

    assert by_key[(legacy_id, LEGACY_ALGORITHM_VERSION)] == (1, Decimal("1.5"))
    assert by_key[(other_id, LEGACY_ALGORITHM_VERSION)] == (1, Decimal("-0.5"))
    assert by_key[(legacy_id, NEXT_VERSION)] == (1, Decimal("2.0"))

    total = sum(count for _, _, _, count, _ in rows)
    assert total == 3, f"сделки потерялись при свёртке: {total} из 3"

    print("  исполненная стратегия и версия алгоритма не схлопываются")

    return bucket_start, rows


async def check_rebuild(session, rollup_crud, bucket_start, before):
    """Повторный проход перезаписывает блок, а не добавляет второй комплект.

    Это свойство ключа свёртки, и расширение ключа могло его сломать:
    достаточно забыть одну колонку в ON CONFLICT, чтобы повтор начал
    вставлять новые строки вместо обновления старых.
    """
    await rollup_crud.build_range(
        bucket_start, bucket_start + BUCKET, BUCKET_MINUTES
    )

    after = await rollup_rows(session, bucket_start)

    assert after == before, (
        f"повторная свёртка изменила блок:\n  было {before}\n  стало {after}"
    )

    print("  повторный проход перезаписывает блок, а не дублирует")


async def check_report_cuts(rollup_crud, legacy_id, other_id):
    """Два разреза отчёта дают разные цифры и не складываются.

    Копибот принадлежит парку legacy, но часть его сделок исполнена
    донором другой стратегии. Разрез по парку отдаёт все три сделки,
    разрез по исполненной — делит их между стратегиями. Сложить эти
    итоги значит посчитать одни и те же сделки дважды.
    """
    since = datetime.now(UTC) - timedelta(hours=1)

    async def total(**filters):
        rows, _, _ = await rollup_crud.profit_by_bot(since=since, **filters)
        return sum(int(row.orders_count or 0) for row in rows)

    by_park = await total(strategy_ids=[legacy_id])
    by_executed_legacy = await total(executed_strategy_ids=[legacy_id])
    by_executed_other = await total(executed_strategy_ids=[other_id])

    assert by_park == 3, f"по парку ожидались три сделки, получено {by_park}"
    assert by_executed_legacy == 2, (
        f"своим алгоритмом исполнены две сделки, получено {by_executed_legacy}"
    )
    assert by_executed_other == 1, (
        f"чужим алгоритмом исполнена одна сделка, получено {by_executed_other}"
    )
    assert by_executed_legacy + by_executed_other == by_park, (
        "разрез по исполненной стратегии обязан покрывать те же сделки"
    )

    assert await total(strategy_ids=[other_id]) == 0, (
        "в парке другой стратегии сделок нет"
    )

    print("  разрезы «по парку» и «по исполненной» расходятся, как задумано")


async def check_empty_donor_chain(session, legacy_id):
    """У обычного бота цепочки доноров нет — и это SQL NULL.

    JSONB-колонка принимает и скаляр `null`, и SQL NULL; выглядят они в
    выводе одинаково, а ведут себя по-разному: `donor_chain IS NULL` не
    видит первый, и отчёт молча считает такую сделку копиботской.
    Поэтому проверяем не «пусто ли», а чем именно пусто.
    """
    moment = datetime.now(UTC)

    await TestOrderCrud(session).bulk_create([
        {
            "asset_symbol": SYMBOL,
            "balance": 1000,
            "order_type": "long",
            "open_price": 100,
            "open_time": moment,
            "open_fee": 0.02,
            "stop_loss_price": 99,
            "close_price": 101,
            "close_time": moment,
            "close_fee": 0.02,
            "profit_loss": Decimal("1.0"),
            "is_active": False,
            "bot_id": DONOR_LEGACY_ID,
            "referral_bot_id": None,
            "donor_chain": None,
            "stop_reason_event": StopReasonEvent.STOP_WON.value,
            "strategy_id": legacy_id,
            "executed_strategy_id": legacy_id,
            "algorithm_version": LEGACY_ALGORITHM_VERSION,
            "created_at": moment,
            "updated_at": moment,
        }
    ])
    await session.commit()

    result = await session.execute(
        text(
            """
            SELECT count(*) FILTER (WHERE donor_chain IS NULL),
                   count(*) FILTER (WHERE donor_chain = 'null'::jsonb)
            FROM test_orders
            WHERE bot_id = :bot_id
            """
        ),
        {"bot_id": DONOR_LEGACY_ID},
    )
    sql_nulls, json_nulls = result.one()

    assert json_nulls == 0, (
        f"{json_nulls} сделок с JSON-скаляром null вместо SQL NULL"
    )
    assert sql_nulls == 1, f"ожидалась одна строка без цепочки, а их {sql_nulls}"

    print("  пустая цепочка доноров пишется как SQL NULL")


async def main():
    if not os.getenv("STRATEGY_ROLLUP_TEST_DB"):
        print(
            "test_strategy_rollup_db пропущен: нужна отдельная база.\n"
            "Как её поднять — в шапке файла."
        )
        return

    print(f"Стратегии в свёртках ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        strategy_crud = StrategyCrud(session)
        legacy_id = await strategy_crud.ensure_legacy()
        other_id = await strategy_crud.ensure("strategy_0", "Прострелы")
        await session.commit()

        assert legacy_id != other_id
        assert await strategy_crud.id_by_key(STRATEGY_LEGACY) == legacy_id

        await prepare_bots(session, legacy_id, other_id)

        rollup_crud = TestOrderRollupCrud(session)
        bucket_start, rows = await check_dimensions(
            session, rollup_crud, legacy_id, other_id
        )
        await check_rebuild(session, rollup_crud, bucket_start, rows)
        await check_report_cuts(rollup_crud, legacy_id, other_id)
        await check_empty_donor_chain(session, legacy_id)

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
