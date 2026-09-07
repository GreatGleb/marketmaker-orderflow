"""id растущих таблиц: int4 -> int8

int4 кончается на 2 147 483 647. На полной скорости парк пишет около 490
строк в секунду в один только `test_orders` — 42 миллиона строк в сутки, то
есть переполнение последовательности примерно через 51 день работы. Ретеншн
от этого не спасает: `DELETE` не откатывает sequence, она считает выданные
значения, а не живые строки. После переполнения падают вставки, а не чтение.

Тут расширяются четыре таблицы, в которые пишет поток: `test_orders`,
`asset_history`, `asset_order_book` и `test_order_rollups`. Внешних ключей
на их `id` нет ни одного, поэтому расширять больше нечего — ни зависимых
колонок, ни каскада.

ВНИМАНИЕ, о стоимости. `ALTER COLUMN ... TYPE` переписывает таблицу целиком
вместе с индексами под `ACCESS EXCLUSIVE` и требует свободного места в её
размер. На пустых таблицах (новое развёртывание) это миллисекунды и ноль
места — ради этого случая миграция и написана. На заполненном сервере, где
`asset_history` и `test_orders` занимают гигабайты, её нельзя запускать без
окна и без свободного места: сначала ретеншн и `RETENTION_REPACK=true`,
потом уже это.

Последовательности расширяются отдельно: `ALTER TABLE` меняет тип колонки,
но `serial`-последовательность остаётся int4 и упрётся в тот же потолок.
Имена у них исторически разъехались с именами таблиц
(`assets_history_id_seq`), поэтому берём их через `pg_get_serial_sequence`.

Revision ID: c93a7d4e28f1
Revises: b7e3d5c81f42
Create Date: 2026-09-08 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c93a7d4e28f1'
down_revision = 'b7e3d5c81f42'
branch_labels = None
depends_on = None


TABLES = (
    'test_orders',
    'asset_history',
    'asset_order_book',
    'test_order_rollups',
)


def _retype(table: str, was: type, becomes: type, seq_type: str) -> None:
    op.alter_column(
        table,
        'id',
        existing_type=was(),
        type_=becomes(),
        existing_nullable=False,
    )
    # Последовательность живёт своим типом и своим потолком, `ALTER TABLE`
    # его не трогает. Без этого шага колонка станет широкой, а выдавать
    # значения перестанут на том же миллиарде.
    op.execute(
        f"""
        DO $$
        DECLARE seq text := pg_get_serial_sequence('{table}', 'id');
        BEGIN
            IF seq IS NOT NULL THEN
                EXECUTE format('ALTER SEQUENCE %s AS {seq_type}', seq);
            END IF;
        END $$;
        """
    )


def upgrade() -> None:
    for table in TABLES:
        _retype(table, sa.Integer, sa.BigInteger, 'bigint')


def downgrade() -> None:
    # Сузить обратно можно только пока ни один id не вышел за int4 —
    # иначе Postgres откажется, и это правильно.
    for table in reversed(TABLES):
        _retype(table, sa.BigInteger, sa.Integer, 'integer')
