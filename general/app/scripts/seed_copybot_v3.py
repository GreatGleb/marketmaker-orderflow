"""Заводит копиботов v3, не трогая остальной парк.

Копибот v3 — третий уровень копирования: он выбирает лучшего копибота v2, тот
лучшего v1, а тот уже обычного бота. Той же цепочкой ходит боевой
`binance_bot` (`_get_best_copy_bot`), поэтому сделки таких ботов — прогноз
результата реальной торговли.

Ботов двое, и различает их только флаг компаундинга: у одного баланс всегда
1000, как у всего парка, у второго он меняется от сделки к сделке.

Отдельный скрипт нужен потому, что `new_bots.py` начинается с
`TRUNCATE test_bots RESTART IDENTITY CASCADE`: им можно только пересоздать
парк целиком, вместе со всей накопленной статистикой. Здесь вставляются
только недостающие боты, поэтому повторный запуск ничего не дублирует.

    python -m app.scripts.seed_copybot_v3           # завести недостающих
    python -m app.scripts.seed_copybot_v3 --dry-run # только показать, что будет

Парк меняется под остановленным симулятором: шарды гасятся через supervisor и
поднимаются обратно. Если симулятор запущен руками, скрипт откажется работать
— иначе часть ботов осталась бы работать со старым составом парка.
"""
import argparse
import asyncio
import logging

from sqlalchemy import select

from app.config import settings
from app.constants.copybot import copybot_v3_rows
from app.crud.test_bot import TestBotCrud
from app.db.base import DatabaseSessionManager
from app.db.models import TestBot
from app.scripts.simulator_flag import SimulatorIsRunning
from app.scripts.supervisor_control import paused

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)


def describe(row: dict) -> str:
    kind = (
        'компаундирующий, 99% счёта'
        if row["copybot_v3_compound_balance"]
        else 'фиксированный баланс 1000'
    )

    return (
        f'окно {row["copybot_v3_time_in_minutes"]} мин, {kind}'
    )


async def missing_rows(session) -> list[dict]:
    """Из задуманной пары — те, кого в базе ещё нет.

    Сверяем по паре (окно, флаг компаундинга), а не по одному лишь признаку
    v3: иначе досев после ручного удаления одного из двух ботов тихо ничего
    бы не сделал.
    """
    existing = (
        await session.execute(
            select(
                TestBot.copybot_v3_time_in_minutes,
                TestBot.copybot_v3_compound_balance,
            ).where(TestBot.copybot_v3_time_in_minutes.is_not(None))
        )
    ).all()

    known = {
        (str(window), bool(compound)) for window, compound in existing
    }

    return [
        row for row in copybot_v3_rows()
        if (
            str(row["copybot_v3_time_in_minutes"]),
            bool(row["copybot_v3_compound_balance"]),
        ) not in known
    ]


async def seed(dry_run: bool = False) -> None:
    dsm = DatabaseSessionManager.create(settings.DB_URL)

    async with dsm.get_session() as session:
        rows = await missing_rows(session)

        if not rows:
            print('✅ Копиботы v3 уже заведены, делать нечего.')
            return

        for row in rows:
            print(f'  + {describe(row)}')

        if dry_run:
            print(f'ℹ️  Создано не будет ничего: это прогон вхолостую.')
            return

        await TestBotCrud(session).bulk_create(rows)
        await session.commit()

        print(f'✅ Копиботов v3 создано: {len(rows)}.')
        print(
            'Симулятор читает состав парка один раз при старте — новые боты '
            'заработают после перезапуска его шардов.'
        )


async def seed_safely(dry_run: bool = False) -> None:
    # Тот же порядок, что в new_bots.py: пока идёт вставка, симулятор
    # остановлен, иначе часть его процессов продолжит работать со старым
    # составом парка.
    if dry_run:
        await seed(dry_run=True)
        return

    with paused("test_bots"):
        await seed()


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Завести копиботов v3, не пересоздавая парк'
    )
    parser.add_argument(
        '-n', '--dry-run', action='store_true',
        help='показать, кого не хватает, и ничего не менять'
    )
    args = parser.parse_args()

    try:
        asyncio.run(seed_safely(dry_run=args.dry_run))
    except SimulatorIsRunning as error:
        print(f'❌ {error}')
        raise SystemExit(1)


if __name__ == "__main__":
    main()
