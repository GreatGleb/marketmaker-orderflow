"""Свёртки test_orders для ретеншна

Одна строка на (десятиминутный блок, бот, пара, реферальный бот). Нужна,
чтобы ретеншн мог удалять сырые сделки, не теряя результаты эксперимента.

Revision ID: b7e3d5c81f42
Revises: a1c4f7b93e21
Create Date: 2026-09-07 19:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e3d5c81f42'
down_revision = 'a1c4f7b93e21'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'test_order_rollups',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
            comment='Date and time of create',
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
            comment='Date and time of update',
        ),
        sa.Column(
            'bucket_start',
            sa.DateTime(timezone=True),
            nullable=False,
            comment='Начало десятиминутного блока, UTC',
        ),
        sa.Column(
            'bot_id',
            sa.Integer(),
            nullable=True,
            comment='Бот, чьи это сделки',
        ),
        sa.Column(
            'referral_bot_id',
            sa.Integer(),
            nullable=True,
            comment='Бот, за которым копировали, если сделки копибота',
        ),
        sa.Column(
            'asset_symbol',
            sa.String(length=255),
            nullable=False,
            comment='Пара',
        ),
        sa.Column(
            'orders_count',
            sa.Integer(),
            nullable=False,
            comment='Сделок в блоке',
        ),
        sa.Column(
            'profitable_count',
            sa.Integer(),
            nullable=False,
            comment='Из них с profit_loss > 0',
        ),
        sa.Column(
            'profit_loss_sum',
            sa.Numeric(),
            nullable=True,
            comment='Суммарный P/L блока',
        ),
        sa.Column(
            'fee_sum',
            sa.Numeric(),
            nullable=True,
            comment='Суммарная комиссия (open_fee + close_fee)',
        ),
        sa.Column(
            'stop_won_count',
            sa.Integer(),
            server_default='0',
            nullable=False,
            comment='Закрыто по цели (stop-won)',
        ),
        sa.Column(
            'stop_loosed_count',
            sa.Integer(),
            server_default='0',
            nullable=False,
            comment='Закрыто по стопу (stop-loosed)',
        ),
        sa.Column(
            'stop_long_lose_count',
            sa.Integer(),
            server_default='0',
            nullable=False,
            comment='Закрыто по времени удержания (stop-long-lose)',
        ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_test_order_rollups_created_at'),
        'test_order_rollups',
        ['created_at'],
        unique=False,
    )
    # Уникальность по ключу свёртки делает пересчёт блока безопасным при
    # любом числе повторов: наложившиеся проходы перезапишут строки, а не
    # положат второй комплект.
    #
    # NULLS NOT DISTINCT — Postgres 15. Без него уникальность не работала бы
    # на строках некопиботов: у них referral_bot_id пустой, а NULL по
    # умолчанию не равен NULL.
    op.create_index(
        'uq_test_order_rollups_key',
        'test_order_rollups',
        ['bucket_start', 'bot_id', 'referral_bot_id', 'asset_symbol'],
        unique=True,
        postgresql_nulls_not_distinct=True,
    )
    op.create_index(
        'idx_test_order_rollups_bot_bucket',
        'test_order_rollups',
        ['bot_id', 'bucket_start'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        'idx_test_order_rollups_bot_bucket', table_name='test_order_rollups'
    )
    op.drop_index(
        'uq_test_order_rollups_key', table_name='test_order_rollups'
    )
    op.drop_index(
        op.f('ix_test_order_rollups_created_at'),
        table_name='test_order_rollups',
    )
    op.drop_table('test_order_rollups')
