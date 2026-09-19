"""Свои наборы пар у стратегий

Второй шаг разделения стратегий ([.ai/docs/test-bots/12-multi-strategy-plan.md],
этап 2). До сих пор список отслеживаемых пар был один на всех, и
пересборка его под одну стратегию неизбежно меняла бы условия у другой:
пара выпадает из `watched_pair` — по ней перестают приходить цены.

`strategy_pairs` — набор пар стратегии. `watched_pair` остаётся общим
техническим списком питателя и собирается как объединение наборов плюс
закреплённые пары активных ботов: поток котировок один на всех, а
потребности у стратегий свои.

`strategies.pair_policy` — как стратегия набирает пары. `manual` —
явный список, `volatility_jumps` — сегодняшний отбор по скачкам с
отсечками по обороту и числу фронтов. Этот отбор принадлежит `legacy`, а
не системе вообще: стратегии прострелов нужны другие пары.

Набор `legacy` заполняется текущим содержимым `watched_pair`, поэтому
сразу после миграции объединение даёт ровно тот же список, что и был, —
ни одна пара не теряется и питатель не перезапускается.

Revision ID: c1f7a26b3d94
Revises: b8d24f0a9c17
Create Date: 2026-09-19 16:00:00.000000

"""
import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision = "c1f7a26b3d94"
down_revision = "b8d24f0a9c17"
branch_labels = None
depends_on = None


LEGACY_KEY = "legacy"
POLICY_VOLATILITY_JUMPS = "volatility_jumps"


def upgrade() -> None:
    op.add_column(
        "strategies",
        sa.Column(
            "pair_policy",
            sa.String(),
            nullable=False,
            server_default="manual",
            comment=(
                "Как стратегия набирает пары: manual — явный список, "
                "volatility_jumps — отбор по скачкам, как у legacy"
            ),
        ),
    )
    # Отбор по скачкам достаётся legacy; остальные стратегии заводятся с
    # явным списком, пока у них нет своего правила.
    op.get_bind().execute(
        sa.text("UPDATE strategies SET pair_policy = :policy WHERE key = :key"),
        {"policy": POLICY_VOLATILITY_JUMPS, "key": LEGACY_KEY},
    )

    op.create_table(
        "strategy_pairs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "strategy_id",
            sa.Integer(),
            nullable=False,
            comment="Чей это набор пар",
        ),
        sa.Column(
            "asset_exchange_id",
            sa.Integer(),
            nullable=False,
            comment="Инструмент из справочника",
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
        sa.ForeignKeyConstraint(
            ["strategy_id"], ["strategies.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["asset_exchange_id"],
            ["asset_exchange_specs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_strategy_pairs_created_at"), "strategy_pairs", ["created_at"]
    )
    op.create_index(
        op.f("ix_strategy_pairs_strategy_id"), "strategy_pairs", ["strategy_id"]
    )
    op.create_index(
        op.f("ix_strategy_pairs_asset_exchange_id"),
        "strategy_pairs",
        ["asset_exchange_id"],
    )
    op.create_index(
        "uq_strategy_pairs_key",
        "strategy_pairs",
        ["strategy_id", "asset_exchange_id"],
        unique=True,
    )

    # Текущий список пар становится набором legacy: объединение наборов
    # обязано дать сразу после миграции ровно то, что было в
    # watched_pair, иначе часть пар осталась бы без котировок.
    op.execute(
        sa.text(
            """
            INSERT INTO strategy_pairs
                (strategy_id, asset_exchange_id, created_at, updated_at)
            SELECT s.id, w.asset_exchange_id, now(), now()
            FROM watched_pair w
            CROSS JOIN strategies s
            WHERE s.key = 'legacy'
              AND w.asset_exchange_id IS NOT NULL
            ON CONFLICT DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_strategy_pairs_key", table_name="strategy_pairs")
    op.drop_index(
        op.f("ix_strategy_pairs_asset_exchange_id"), table_name="strategy_pairs"
    )
    op.drop_index(
        op.f("ix_strategy_pairs_strategy_id"), table_name="strategy_pairs"
    )
    op.drop_index(
        op.f("ix_strategy_pairs_created_at"), table_name="strategy_pairs"
    )
    op.drop_table("strategy_pairs")
    op.drop_column("strategies", "pair_policy")
