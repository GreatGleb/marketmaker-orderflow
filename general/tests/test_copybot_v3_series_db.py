"""Ряд для кривой счёта копибота v3 — на настоящей базе.

`profit_series` склеивает два источника: свёртки за всё, что уже свёрнуто, и
сырые сделки за хвост после watermark. Проверять это на заглушках
бессмысленно, вся суть в SQL — и цена ошибки высокая сразу по двум причинам.

Во-первых, колонки в источниках называются по-разному: в свёртке
`profit_loss_sum`, в сырье `profit_loss`. Перепутать легко, а заметить нельзя:
пока свёрток нет (первые трое суток жизни базы), ветка свёрток просто не
выполняется, и запрос выглядит рабочим.

Во-вторых, сетка блоков. `date_bin` здесь обязан резать сырьё ровно так же,
как `build_range` режет свёртки, — с тем же интервалом и origin `epoch`.
Сдвинь origin, и блоки хвоста встанут между блоками свёрток: ряд поедет на
границе свёрнутого, а граница каждый час уезжает.

Сходимость проверяется сравнением с `profit_by_bot`: эти двое отвечают на
вопросы «когда сколько» и «сколько всего» об одних и тех же сделках, и их
суммы обязаны совпадать. Разойдутся — и в отчёте кривая счёта не сойдётся с
показанным рядом же P/L.

База нужна отдельная: тест чистит `test_orders`, `test_order_rollups` и
`test_bots`, то есть на рабочей снёс бы накопленную статистику.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists v3_check' \\
        -c 'create database v3_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/v3_check \\
      -e V3_SERIES_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_copybot_v3_series_db

Схема накатывается `alembic upgrade head` — тест её не создаёт.

Без `V3_SERIES_TEST_DB` проверка пропускается.
"""
import asyncio
import os

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from app.config import settings
from app.crud.test_order_rollup import TestOrderRollupCrud, floor_to_bucket
from app.db.base import DatabaseSessionManager

UTC = timezone.utc

BOT_ID = 990001

# Одна сделка заведомо старше суток — она уйдёт в свёртки; вторая свежая —
# останется в сыром хвосте. Так задействованы обе ветки сразу.
ROLLED_UP = (timedelta(hours=30), Decimal("5.5"))
RAW = (timedelta(hours=3), Decimal("-2.25"))

INSERT_BOT = """
    insert into test_bots (
        id, symbol, balance, is_active,
        copybot_v3_time_in_minutes, copybot_v3_compound_balance,
        consider_ma_for_open_order, consider_ma_for_close_order,
        copybot_v1_check_for_24h_profitability,
        copybot_v1_exclude_losing_donors, created_at, updated_at
    ) values (
        :id, '', 1000, true, 720, true, false, false, false, false,
        now(), now()
    )
"""

INSERT_ORDER = """
    insert into test_orders (
        bot_id, asset_symbol, order_type, balance, open_price, close_price,
        profit_loss, open_fee, close_fee, stop_loss_price, is_active,
        created_at, updated_at, open_time, close_time
    ) values (
        :bot, 'TESTUSDT', 'buy', 990, 100, 101, :pnl, 0.5, 0.5, 99, false,
        :at, :at, :at, :at
    )
"""


async def seed(session, now):
    for table in ("test_order_rollups", "test_orders", "test_bots"):
        await session.execute(text(f"delete from {table}"))

    await session.execute(text(INSERT_BOT), {"id": BOT_ID})

    for ago, pnl in (ROLLED_UP, RAW):
        await session.execute(
            text(INSERT_ORDER),
            {"bot": BOT_ID, "pnl": pnl, "at": now - ago},
        )

    await session.commit()


async def build_rollups(session, now) -> int:
    """Свернуть всё старше суток — ровно то, что делает воркер."""
    bucket = timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES)
    crud = TestOrderRollupCrud(session)

    written = await crud.build_range(
        start=floor_to_bucket(now - timedelta(days=3), bucket),
        end=floor_to_bucket(now - timedelta(days=1), bucket),
        bucket_minutes=settings.ROLLUP_BUCKET_MINUTES,
    )
    await session.commit()

    return written


async def check_series_matches_totals(session, now):
    crud = TestOrderRollupCrud(session)
    since = now - timedelta(days=3)

    watermark = await crud.rollup_watermark()

    assert watermark is not None, (
        "свёрток нет — тогда ветка свёрток не выполнится, и проверять нечего"
    )

    rows, _, _ = await crud.profit_by_bot(
        since=since, just_copy_bots_v3=True
    )
    total = sum(Decimal(str(row.profit_loss or 0)) for row in rows)

    series = await crud.profit_series(bot_id=BOT_ID, since=since)
    series_total = sum(Decimal(str(row.profit_loss or 0)) for row in series)

    print(f"  watermark: {watermark:%Y-%m-%d %H:%M}")
    print(f"  всего по агрегату: {total}")
    print(f"  всего по ряду    : {series_total}")

    assert series, "ряд пустой: кривую счёта строить не из чего"
    assert len(series) == 2, (
        f"блоков в ряду {len(series)}, ожидалось 2 (свёрнутый и сырой). "
        f"Одинаковый bucket_start у обеих сделок означает, что сетка блоков "
        f"в ветках разъехалась"
    )
    assert series_total == total, (
        f"ряд даёт {series_total}, агрегат {total}: в отчёте кривая счёта не "
        f"сойдётся с показанным рядом же P/L"
    )

    bucket = timedelta(minutes=settings.ROLLUP_BUCKET_MINUTES)

    for row in series:
        assert row.bucket_start == floor_to_bucket(row.bucket_start, bucket), (
            f"блок {row.bucket_start} не лежит на границе сетки: сырой хвост "
            f"считается не тем origin, что свёртки"
        )

    print("  обе ветки сходятся, блоки на сетке")


async def check_rolled_up_half_is_really_from_rollups(session, now):
    """Свёрнутая сделка приходит именно из свёрток, а не из сырья."""
    await session.execute(text(
        "delete from test_orders where created_at < :cutoff"
    ), {"cutoff": now - timedelta(days=1)})
    await session.commit()

    crud = TestOrderRollupCrud(session)
    series = await crud.profit_series(
        bot_id=BOT_ID, since=now - timedelta(days=3)
    )
    total = sum(Decimal(str(row.profit_loss or 0)) for row in series)

    expected = ROLLED_UP[1] + RAW[1]

    print(f"  после удаления сырья старше суток: {total}")

    assert total == expected, (
        f"получилось {total}, ожидалось {expected}. Сырьё за свёрнутый период "
        f"удалено — ровно так делает ретеншн, — значит {ROLLED_UP[1]} может "
        f"прийти только из свёрток. Не сошлось: ветка свёрток не работает, и "
        f"кривая обрывается на сроке хранения сырых сделок"
    )

    print("  свёрнутая часть читается из свёрток")


async def main():
    if not os.getenv("V3_SERIES_TEST_DB"):
        print(
            "test_copybot_v3_series_db пропущен: нужна отдельная база.\n"
            "Как её поднять — в шапке файла."
        )
        return

    print(f"Ряд копибота v3 на живой базе "
          f"({settings.DB_URL.rsplit('/', 1)[-1]}):")

    now = datetime.now(UTC)
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        await seed(session, now)
        written = await build_rollups(session, now)

        print(f"  свёрток записано: {written}")

        await check_series_matches_totals(session, now)
        await check_rolled_up_half_is_really_from_rollups(session, now)

    print("\nвсё сходится")


if __name__ == "__main__":
    asyncio.run(main())
