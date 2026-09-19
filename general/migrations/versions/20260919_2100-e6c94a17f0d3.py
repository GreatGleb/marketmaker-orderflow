"""Явный вид бота в bot_kind

Тип бота нигде не хранился: он выводился из того, какая из колонок-маркеров
не NULL (`COPYBOT_MARKER_COLUMNS`). Пока видов было два, это работало;
цена такого решения описана прямо в коде — забытая в списке колонка
означает, что новый копибот попадёт в пул доноров обычных ботов, цепочка
замкнётся в кольцо, а донор соберётся из нулей. Молча, без ошибок в логах.

`bot_kind` делает вид явным. Колонки-маркеры остаются: в них лежат окна
оценки прибыльности, и они нужны по существу, а не как признак типа.

Неоднозначные строки (заполнено больше одного маркера) не редкость в
теории и ошибка на практике: такой бот сейчас ведёт себя как самый
старший из своих маркеров. Миграция раскладывает их по тому же правилу
— v3, затем v2, затем v1 — и сообщает, сколько их было, чтобы это можно
было разобрать глазами, а не обнаружить потом в отчётах.

Revision ID: e6c94a17f0d3
Revises: d3b81c5e47a2
Create Date: 2026-09-19 21:00:00.000000

"""
import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "e6c94a17f0d3"
down_revision = "d3b81c5e47a2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "test_bots",
        sa.Column(
            "bot_kind",
            sa.String(),
            nullable=False,
            server_default="ordinary",
            comment=(
                "Вид бота: ordinary, copy_v1, copy_v2, copy_v3. Раньше "
                "выводился из того, какая колонка-маркер не NULL"
            ),
        ),
    )

    connection = op.get_bind()

    ambiguous = connection.execute(
        sa.text(
            """
            SELECT count(*) FROM test_bots
            WHERE (copy_bot_min_time_profitability_min IS NOT NULL)::int
                + (copybot_v2_time_in_minutes IS NOT NULL)::int
                + (copybot_v3_time_in_minutes IS NOT NULL)::int > 1
            """
        )
    ).scalar_one()

    if ambiguous:
        print(
            f"⚠️  Ботов с несколькими колонками-маркерами: {ambiguous}. "
            f"Вид проставлен по старшему маркеру — так же, как их вёл "
            f"симулятор. Разберите их отдельно."
        )

    connection.execute(
        sa.text(
            """
            UPDATE test_bots SET bot_kind = CASE
                WHEN copybot_v3_time_in_minutes IS NOT NULL THEN 'copy_v3'
                WHEN copybot_v2_time_in_minutes IS NOT NULL THEN 'copy_v2'
                WHEN copy_bot_min_time_profitability_min IS NOT NULL
                    THEN 'copy_v1'
                ELSE 'ordinary'
            END
            """
        )
    )

    op.create_index(op.f("ix_test_bots_bot_kind"), "test_bots", ["bot_kind"])


def downgrade() -> None:
    op.drop_index(op.f("ix_test_bots_bot_kind"), table_name="test_bots")
    op.drop_column("test_bots", "bot_kind")
