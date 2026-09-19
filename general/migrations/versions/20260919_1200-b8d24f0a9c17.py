"""Принадлежность ботов и сделок к стратегии

Первый шаг разделения стратегий ([.ai/docs/test-bots/12-multi-strategy-plan.md]).
До сих пор алгоритм был один, и вопрос «по каким правилам получена эта
сделка» не возникал. С появлением стратегии 0 он становится главным: без
ответа на него результаты двух алгоритмов складываются в один отчёт и
сравнивать их нечем.

`strategies` — регистрация стратегии, а не её реализация. Реализация
выбирается из реестра Python по `key`; строка нужна, чтобы на стратегию
могли ссылаться боты и сделки, и чтобы новые входы можно было запретить,
не трогая код.

У сделок два измерения, а не одно. `strategy_id` — парк, которому
принадлежит бот, `executed_strategy_id` — по какому алгоритму сделка
фактически исполнена. У обычного бота они совпадают; у копибота второе
определяет конечный донор, и с общим пулом доноров донор может
принадлежать другой стратегии. Разрезы не суммируются друг с другом:
одна и та же сделка попадает в оба.

`algorithm_version` у старых строк остаётся NULL намеренно. Приписать
истории сегодняшнюю версию — значит стереть границу между сделками до
сентябрьских исправлений свежести цены и после.

Колонки добавляются со значением по умолчанию, поэтому `ADD COLUMN` не
переписывает таблицу (Postgres 11+) — иначе 42 миллиона строк `test_orders`
задержали бы старт контейнера. Умолчание остаётся после миграции: в очереди
Redis могут лежать сделки, записанные прошлой версией симулятора, и без
умолчания их вставка упала бы.

Внешние ключи на `test_orders` и `test_order_rollups` создаются NOT VALID:
проверка существующих строк прочитала бы таблицу целиком. Новые строки
проверяются с первой же вставки; при желании старые можно проверить потом
через VALIDATE CONSTRAINT, он не блокирует запись.

Ключ свёртки расширяется новыми измерениями. Это не формальность:
копибот может сменить донора на бота другой стратегии внутри одного
блока, и без измерений в ключе две такие группы схлопнулись бы в одну
строку — вторая перезаписала бы первую, и часть сделок исчезла бы из
статистики. Пересоздание индекса блокирует запись в свёртки на время
своей работы; воркер свёрток запускается раз в несколько минут и
переживает это ожидание.

Revision ID: b8d24f0a9c17
Revises: a7c9e14b6d31
Create Date: 2026-09-19 12:00:00.000000

"""
import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "b8d24f0a9c17"
down_revision = "a7c9e14b6d31"
branch_labels = None
depends_on = None


LEGACY_KEY = "legacy"

# Колонки-маркеры копибота: тип бота отдельным полем не хранится, он
# выводится из того, какая из них не NULL (COPYBOT_MARKER_COLUMNS в
# app/crud/test_bot.py). Здесь они нужны, чтобы проставить пул доноров
# только копиботам.
COPYBOT_MARKERS = (
    "copy_bot_min_time_profitability_min",
    "copybot_v2_time_in_minutes",
    "copybot_v3_time_in_minutes",
)


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "key",
            sa.String(),
            nullable=False,
            comment="Технический ключ: legacy, strategy_0",
        ),
        sa.Column(
            "title",
            sa.String(),
            nullable=False,
            comment="Человекочитаемое название",
        ),
        sa.Column(
            "allows_new_entries",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
            comment=(
                "Разрешены ли новые входы. Снятый признак не закрывает уже "
                "открытые позиции: их доводит тот же алгоритм, с которым "
                "они открывались"
            ),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Date and time of create",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
            comment="Date and time of update",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key"),
    )
    op.create_index(
        op.f("ix_strategies_created_at"), "strategies", ["created_at"]
    )

    legacy_id = op.get_bind().execute(
        sa.text(
            """
            INSERT INTO strategies (key, title, allows_new_entries)
            VALUES (:key, :title, true)
            RETURNING id
            """
        ),
        {"key": LEGACY_KEY, "title": "Текущий алгоритм"},
    ).scalar_one()

    default = sa.text(str(legacy_id))

    op.add_column(
        "test_bots",
        sa.Column(
            "strategy_id",
            sa.Integer(),
            nullable=False,
            server_default=default,
            comment="Стратегия, экземпляром которой является бот",
        ),
    )
    op.add_column(
        "test_bots",
        sa.Column(
            "donor_scope",
            postgresql.JSONB(),
            nullable=True,
            comment=(
                "Пул доноров копибота: {'mode': 'all'} либо "
                "{'mode': 'list', 'strategies': ['legacy']}. NULL у "
                "обычных ботов и означает отсутствие ограничений"
            ),
        ),
    )
    op.create_index(
        op.f("ix_test_bots_strategy_id"), "test_bots", ["strategy_id"]
    )
    op.create_foreign_key(
        "fk_test_bots_strategy_id",
        "test_bots",
        "strategies",
        ["strategy_id"],
        ["id"],
    )

    # Существующим копиботам — явный пул из одной стратегии, а не "all".
    # Молча расширить их выбор значило бы поменять идущий эксперимент:
    # статистика до и после стала бы несравнимой без единой записи об этом.
    marker_is_set = " OR ".join(
        f"{column} IS NOT NULL" for column in COPYBOT_MARKERS
    )
    op.execute(
        sa.text(
            f"""
            UPDATE test_bots
            SET donor_scope = '{{"mode": "list", "strategies": ["{LEGACY_KEY}"]}}'::jsonb
            WHERE {marker_is_set}
            """
        )
    )

    for table in ("test_orders", "test_order_rollups"):
        op.add_column(
            table,
            sa.Column(
                "strategy_id",
                sa.Integer(),
                nullable=False,
                server_default=default,
                comment="Стратегия парка бота",
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "executed_strategy_id",
                sa.Integer(),
                nullable=False,
                server_default=default,
                comment=(
                    "Стратегия, по которой сделка фактически исполнена; "
                    "у копибота её определяет конечный донор"
                ),
            ),
        )
        op.add_column(
            table,
            sa.Column(
                "algorithm_version",
                sa.String(),
                nullable=True,
                comment=(
                    "Версия алгоритма на момент открытия позиции; NULL у "
                    "строк, записанных до появления поля"
                ),
            ),
        )
        for column in ("strategy_id", "executed_strategy_id"):
            op.execute(
                sa.text(
                    f"""
                    ALTER TABLE {table}
                    ADD CONSTRAINT fk_{table}_{column}
                    FOREIGN KEY ({column}) REFERENCES strategies (id)
                    NOT VALID
                    """
                )
            )

    op.add_column(
        "test_orders",
        sa.Column(
            "donor_chain",
            postgresql.JSONB(),
            nullable=True,
            comment=(
                "Цепочка id доноров от копибота к обычному боту, например "
                "[v2_id, v1_id]; NULL у обычных ботов"
            ),
        ),
    )

    op.drop_index("uq_test_order_rollups_key", table_name="test_order_rollups")
    op.create_index(
        "uq_test_order_rollups_key",
        "test_order_rollups",
        [
            "bucket_start",
            "bot_id",
            "referral_bot_id",
            "asset_symbol",
            "strategy_id",
            "executed_strategy_id",
            "algorithm_version",
        ],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )


def downgrade() -> None:
    op.drop_index("uq_test_order_rollups_key", table_name="test_order_rollups")
    op.create_index(
        "uq_test_order_rollups_key",
        "test_order_rollups",
        ["bucket_start", "bot_id", "referral_bot_id", "asset_symbol"],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )

    op.drop_column("test_orders", "donor_chain")

    for table in ("test_order_rollups", "test_orders"):
        for column in ("strategy_id", "executed_strategy_id"):
            op.drop_constraint(f"fk_{table}_{column}", table, type_="foreignkey")
        op.drop_column(table, "algorithm_version")
        op.drop_column(table, "executed_strategy_id")
        op.drop_column(table, "strategy_id")

    op.drop_constraint("fk_test_bots_strategy_id", "test_bots", type_="foreignkey")
    op.drop_index(op.f("ix_test_bots_strategy_id"), table_name="test_bots")
    op.drop_column("test_bots", "donor_scope")
    op.drop_column("test_bots", "strategy_id")

    op.drop_index(op.f("ix_strategies_created_at"), table_name="strategies")
    op.drop_table("strategies")
