"""Копибот v3 считает разорения, а не останавливается навсегда

Revision ID: b3f81a6c4d29
Revises: f4a17c98b2e0
Create Date: 2026-09-22 21:15:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b3f81a6c4d29'
down_revision = 'f4a17c98b2e0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'test_bots',
        sa.Column(
            'copybot_v3_ruins',
            sa.Integer(),
            nullable=False,
            server_default='0',
            comment=(
                'How many times the account was wiped out and restored to '
                'the starting balance'
            ),
        ),
    )
    op.add_column(
        'test_bots',
        sa.Column(
            'copybot_v3_last_ruin_at',
            sa.DateTime(timezone=True),
            nullable=True,
            comment='When the account was last wiped out and restored',
        ),
    )

    # Прежние остановки — это ровно первое разорение, просто тогда оно было
    # последним событием в жизни бота. Переносим их в новый счётчик, иначе
    # накопленный факт «счёт кончился такого-то числа» потеряется, а отчёт
    # покажет ноль разорений у бота, который на самом деле уже не торгует.
    op.execute(
        """
        UPDATE test_bots
        SET copybot_v3_ruins = 1,
            copybot_v3_last_ruin_at = copybot_v3_stopped_at
        WHERE copybot_v3_stopped_at IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column('test_bots', 'copybot_v3_last_ruin_at')
    op.drop_column('test_bots', 'copybot_v3_ruins')
