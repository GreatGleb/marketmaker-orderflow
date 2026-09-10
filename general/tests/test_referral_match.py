"""Отчёт по донорам копибота: воспроизводит ли он отбор воркера.

Смысл отчёта в том, что он считает донора *тем же способом*, что и
`ProfitableBotUpdaterCommand.filter_profitable_bots_id`. Если два определения
разъедутся, отчёт начнёт показывать расхождения там, где их нет, — и станет
хуже, чем бесполезным: по нему будут чинить исправное.

Проверить это на заглушках нельзя — весь отбор в SQL, с боковыми соединениями
и оконными функциями. Поэтому тест засевает свёртки в настоящую базу и
смотрит на места, которые отчёт выдал взятым донорам.

Главное, что тут закодировано, — правило отсева доноров. Оно перевёрнутое:
фильтр выбрасывает тех, у кого копиры **ушли в минус**, и не трогает тех, у
кого истории копирования нет вовсе. Раньше было наоборот, требовалась
положительная донорская прибыль, и «истории нет» приравнивалось к «плохой» —
из-за чего пул кандидатов схлопывался с пятнадцати тысяч до нескольких
десятков инкумбентов. Проверку на это держит `check_exclude_losing_donors`.

База нужна отдельная: тест чистит `test_order_rollups` и `test_bots`
целиком, а на рабочей базе это снесло бы парк.

    docker exec orderflow_postgres psql -U postgres \\
        -c 'drop database if exists donor_check' \\
        -c 'create database donor_check'

    docker run --rm --network marketmaker-orderflow_orderflow_network \\
      -e DB_URL=postgresql+asyncpg://postgres:secret@db:5432/donor_check \\
      -e DONOR_TEST_DB=1 -e PYTHONPATH=/opt/services/app/src \\
      -v "$PWD/general:/opt/services/app/src" -w /opt/services/app/src \\
      --entrypoint python marketmaker-orderflow-general \\
      -m tests.test_referral_match

Схема накатывается `alembic upgrade head` — тест её не создаёт.
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
BUCKET = timedelta(minutes=10)
WINDOW_MINUTES = 30

# Копиботы: у всех одно окно в 30 минут, различаются только флагами. Так
# видно, что расхождение в местах даёт именно флаг, а не что-то ещё.
PLAIN, WITH_24H, NO_LOSING = 1, 2, 3
# Кандидаты в доноры, по убыванию собственной прибыли за окно.
BEST, NOHIST, SECOND, LOSER = 101, 104, 102, 103

INSERT_BOT = """
    INSERT INTO test_bots (
        id, symbol, balance, is_active,
        copy_bot_min_time_profitability_min,
        copybot_v1_check_for_24h_profitability,
        copybot_v1_exclude_losing_donors,
        created_at, updated_at
    ) VALUES (
        :id, 'TESTUSDT', 1000, true, :tf, :check_24h, :no_losing, :now, :now
    )
"""

INSERT_ROLLUP = """
    INSERT INTO test_order_rollups (
        bucket_start, bot_id, referral_bot_id, asset_symbol,
        orders_count, profitable_count, profit_loss_sum, fee_sum,
        stop_won_count, stop_loosed_count, stop_long_lose_count,
        created_at, updated_at
    ) VALUES (
        :bucket_start, :bot_id, :referral_bot_id, 'TESTUSDT',
        :orders_count, 0, :profit_loss, 0, 0, 0, 0, :now, :now
    )
"""


async def seed(session, now):
    """Расклад, в котором правильный ответ известен заранее.

    Точка оценки — `now`. Окно в 30 минут накрывает три предыдущих блока.
    По собственной прибыли за это окно: BEST 9, NOHIST 6, SECOND 3,
    LOSER −3 (в отбор не проходит вовсе).

    Дальше по подвоху на каждый флаг:

    * BEST пять часов назад потерял столько, что за сутки уходит в минус.
      Копибот с `check_24h` обязан его отбросить;
    * за BEST копировали в убыток, за SECOND — в плюс, а NOHIST не копировал
      никто. Копибот с `exclude_losing` обязан отбросить BEST и **оставить
      NOHIST**: отсутствие истории — не повод дисквалифицировать.

    NOHIST здесь и есть вся суть правки. По старому правилу («требовать
    положительную донорскую прибыль») он вылетал вместе с BEST, и донором
    становился SECOND — единственный с историей. По новому донором
    становится NOHIST, потому что он прибыльнее и ничем себя не
    скомпрометировал.
    """
    # CASCADE обязателен: на test_bots ссылается test_orders. В этой базе
    # сделок нет, но без CASCADE Postgres откажется чистить сам справочник.
    await session.execute(
        text("TRUNCATE test_order_rollups, test_bots CASCADE")
    )

    for bot_id, check_24h, no_losing in (
        (PLAIN, False, False),
        (WITH_24H, True, False),
        (NO_LOSING, False, True),
    ):
        await session.execute(text(INSERT_BOT), dict(
            id=bot_id, tf=WINDOW_MINUTES, check_24h=check_24h,
            no_losing=no_losing, now=now,
        ))

    for bot_id in (BEST, NOHIST, SECOND, LOSER):
        await session.execute(text(INSERT_BOT), dict(
            id=bot_id, tf=None, check_24h=False, no_losing=False, now=now,
        ))

    rows = []

    def add(bucket, bot_id, profit, referral=None, orders=1):
        rows.append(dict(
            bucket_start=bucket, bot_id=bot_id, referral_bot_id=referral,
            orders_count=orders, profit_loss=Decimal(str(profit)), now=now,
        ))

    for step in (3, 2, 1):
        bucket = now - BUCKET * step
        add(bucket, BEST, 3)
        add(bucket, NOHIST, 2)
        add(bucket, SECOND, 1)
        add(bucket, LOSER, -1)

        # Чем кончилось копирование. За NOHIST не копировал никто — строк с
        # referral_bot_id = NOHIST нет ни одной, и это не упущение фикстуры,
        # а сам проверяемый случай.
        add(bucket, NO_LOSING, -5, referral=BEST)
        add(bucket, NO_LOSING, 2, referral=SECOND)

    # Пять часов назад BEST провалился. В тридцатиминутное окно это не
    # попадает, в суточное — попадает.
    add(now - timedelta(hours=5), BEST, -100)

    # Сами сделки, которые разбирает отчёт: каждый копибот взял по донору.
    add(now, PLAIN, 1, referral=BEST, orders=10)
    add(now, PLAIN, 1, referral=SECOND, orders=20)
    add(now, PLAIN, 1, referral=LOSER, orders=30)
    add(now, WITH_24H, 1, referral=BEST, orders=40)
    add(now, NO_LOSING, 1, referral=BEST, orders=50)
    add(now, NO_LOSING, 1, referral=NOHIST, orders=60)

    for row in rows:
        await session.execute(text(INSERT_ROLLUP), row)

    await session.commit()

    return len(rows)


async def verdicts_at(crud, now, bot_id, tf, check_24h, exclude_losing):
    """{донор: (место, кто должен был быть)} для одного копибота в блоке now."""
    rows = await crud.donor_match(
        tf=tf, check_24h=check_24h, exclude_losing=exclude_losing,
        since=now, until=now + BUCKET,
    )

    return {
        row.referral_bot_id: (row.donor_rank, row.expected_bot_id)
        for row in rows if row.copy_bot_id == bot_id
    }


async def check_plain(crud, now):
    """Без флагов: места расставляются по сумме P/L за окно."""
    got = await verdicts_at(crud, now, PLAIN, WINDOW_MINUTES, False, False)

    assert got[BEST] == (1, BEST), f"BEST должен быть первым, получено {got[BEST]}"
    assert got[SECOND] == (3, BEST), (
        f"SECOND должен быть третьим (за BEST и NOHIST), получено {got[SECOND]}"
    )

    rank, expected = got[LOSER]
    assert rank is None, f"убыточный донор не должен проходить отбор, место {rank}"
    assert expected == BEST, f"ожидался BEST, а не {expected}"

    print("    без флагов: BEST место 1, SECOND место 3, "
          "LOSER в отбор не прошёл")


async def check_24h_filter(crud, now):
    """С проверкой за сутки лидер окна выбывает, и правильный ответ другой."""
    got = await verdicts_at(crud, now, WITH_24H, WINDOW_MINUTES, True, False)

    rank, expected = got[BEST]

    assert rank is None, (
        f"BEST за сутки в минусе и не должен проходить отбор, место {rank}"
    )
    assert expected == NOHIST, (
        f"с проверкой за сутки донором должен быть NOHIST, а не {expected}"
    )
    print("    с проверкой за сутки: BEST выбыл (за сутки в минусе), "
          "правильный донор — NOHIST")


async def check_exclude_losing_donors(crud, now):
    """Отсев доноров выбрасывает провалившихся и не трогает непроверенных.

    Ради этой проверки правка и делалась. Раньше фильтр требовал
    положительной донорской прибыли, и NOHIST вылетал вместе с BEST —
    просто потому, что за ним никто не копировал. Донором становился
    SECOND, единственный с историей, и так пул схлопывался до инкумбентов.
    """
    got = await verdicts_at(crud, now, NO_LOSING, WINDOW_MINUTES, False, True)

    rank, expected = got[BEST]
    assert rank is None, (
        f"за BEST копировали в убыток, он не должен проходить, место {rank}"
    )

    assert expected == NOHIST, (
        f"донором должен быть NOHIST — он прибыльнее SECOND и ничем себя "
        f"не скомпрометировал; получено {expected}"
    )

    nohist_rank, _ = got[NOHIST]
    assert nohist_rank == 1, (
        f"бот без истории копирования обязан проходить отбор, "
        f"а получил место {nohist_rank}"
    )

    print("    с отсевом провалившихся: BEST выбыл (копиры в минусе), "
          "а NOHIST без истории прошёл и занял место 1")
    print("      (по старому правилу «требовать плюс» тут победил бы SECOND)")


async def check_no_donor_at_all(crud, now):
    """Когда прибыльных ботов нет вовсе, отчёт говорит именно это.

    Это не ошибка копибота: воркер по устройству оставляет в Redis прежний
    ключ, и сделки идут по старому конфигу (08-gotchas.md, пункт 10b).
    Отличать этот случай от «взял не того» обязательно, иначе отчёт будет
    обвинять копибота в поведении воркера.
    """
    quiet = now + BUCKET * 100

    await crud.session.execute(text(INSERT_ROLLUP), dict(
        bucket_start=quiet, bot_id=PLAIN, referral_bot_id=BEST,
        orders_count=7, profit_loss=Decimal("1"), now=now,
    ))
    await crud.session.commit()

    got = await verdicts_at(
        crud, quiet, PLAIN, WINDOW_MINUTES, False, False
    )
    rank, expected = got[BEST]

    assert rank is None, f"в тишине никто не должен проходить, место {rank}"
    assert expected is None, f"подходящих доноров быть не должно, а есть {expected}"
    print("    в блоке без прибыльных ботов: донора не было, "
          "отчёт не считает это промахом копибота")


async def run_checks(session, now):
    seeded = await seed(session, now)
    print(f"  засеяно {seeded} строк свёрток")

    crud = TestOrderRollupCrud(session)

    await check_plain(crud, now)
    await check_24h_filter(crud, now)
    await check_exclude_losing_donors(crud, now)
    await check_no_donor_at_all(crud, now)


async def main():
    print("Отчёт по донорам копибота:")

    if not os.getenv("DONOR_TEST_DB"):
        print(
            "  проверка пропущена: нужна отдельная база.\n"
            "  Как её поднять — в шапке файла."
        )
        return

    print(f"  на живой базе ({settings.DB_URL.rsplit('/', 1)[-1]}):")

    dsm = DatabaseSessionManager.create(settings.DB_URL)

    # Точка оценки обязана лежать ровно на границе блока: свёртки бьются
    # date_bin'ом от эпохи, и точка посреди блока сдвинула бы все окна.
    now = floor_to_bucket(datetime.now(UTC), BUCKET)

    async with dsm.get_session() as session:
        await run_checks(session, now)

    print("  ✅ все проверки прошли")


if __name__ == "__main__":
    asyncio.run(main())
