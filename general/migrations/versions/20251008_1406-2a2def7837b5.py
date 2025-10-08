"""Add use_trailing_stop to test_bots

Revision ID: 2a2def7837b5
Revises: f5143817785b
Create Date: 2025-10-08 14:06:45.507124

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql.expression import table, column, and_


# revision identifiers, used by Alembic.
revision = '2a2def7837b5'
down_revision = 'f5143817785b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'test_bots',
        sa.Column(
            'use_trailing_stop',
            sa.Boolean(),
            nullable=True,
            server_default=sa.false(),
            comment="True, если используются трейлинг-стопы; False, если фиксированные стоп-лоссы."
        )
    )

    test_bots_table = table(
        'test_bots',
        column('use_trailing_stop', sa.Boolean),
        column('copy_bot_min_time_profitability_min', sa.Numeric),
        column('copybot_v2_time_in_minutes', sa.Numeric)
    )

    op.execute(
        test_bots_table.update().where(
            and_(
                test_bots_table.c.copy_bot_min_time_profitability_min.is_(None),
                test_bots_table.c.copybot_v2_time_in_minutes.is_(None)
            )
        ).values(use_trailing_stop=True)
    )



def downgrade() -> None:
    op.drop_column('test_bots', 'use_trailing_stop')
