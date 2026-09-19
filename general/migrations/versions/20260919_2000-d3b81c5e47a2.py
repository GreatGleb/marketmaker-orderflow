"""Свои настройки стратегии в strategy_config

Последний кусок этапа 4 ([.ai/docs/test-bots/12-multi-strategy-plan.md]).
Без него добавление стратегии упиралось в схему: каждый новый параметр
означал колонку в `test_bots` и миграцию, а параметры одной стратегии
висели бы в таблице у всех остальных.

`strategy_config` — параметры, специфичные для алгоритма. Общие поля
бота (баланс, признаки копирования, активность) остаются колонками:
по ним ходят отбор доноров, отчёты и шардирование, и прятать их в JSON
значило бы потерять индексы.

Валидацию делает сам алгоритм (`Algorithm.parse_config`), а версия
схемы лежит в самом документе (`schema_version`). Версия схемы конфига
и версия алгоритма — разные вещи: первая говорит, как читать параметры,
вторая — по каким правилам получена сделка.

`legacy` своих настроек не имеет: у него всё в колонках, и колонка
остаётся NULL. Это не временная мера — переносить его параметры в JSON
значило бы переписать отбор доноров и отчёты ради формы хранения.

Revision ID: d3b81c5e47a2
Revises: c1f7a26b3d94
Create Date: 2026-09-19 20:00:00.000000

"""
import sqlalchemy as sa

from alembic import op
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "d3b81c5e47a2"
down_revision = "c1f7a26b3d94"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "test_bots",
        sa.Column(
            "strategy_config",
            postgresql.JSONB(),
            nullable=True,
            comment=(
                "Настройки, специфичные для алгоритма стратегии, с ключом "
                "schema_version. NULL — у стратегии своих настроек нет "
                "(как у legacy: все его параметры лежат колонками)"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("test_bots", "strategy_config")
