"""create market_orders table

Revision ID: 7611324487bd
Revises: 95bf45f04825
Create Date: 2025-07-16 11:58:05.514859

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '7611324487bd'
down_revision = '95bf45f04825'
branch_labels = None
depends_on = None




def upgrade():
    """
    Создает таблицу market_orders и соответствующие индексы.
    """
    op.create_table(
        'market_orders',
        sa.Column('id', sa.Integer, autoincrement=True, primary_key=True),
        sa.Column('client_order_id', sa.String, nullable=True),
        sa.Column('exchange_name', sa.String, nullable=True),
        sa.Column('exchange_order_id', sa.String, nullable=True),
        sa.Column('symbol', sa.String, nullable=True),
        sa.Column('side', sa.String, nullable=True),
        sa.Column('position_side', sa.String, nullable=True),
        sa.Column('quote_quantity', sa.Numeric, nullable=True),
        sa.Column('asset_quantity', sa.Numeric, nullable=True),
        sa.Column('open_order_type', sa.String, nullable=True),
        sa.Column('close_order_type', sa.String, nullable=True),
        sa.Column('start_price', sa.Numeric, nullable=True),
        sa.Column('activation_price', sa.Numeric, nullable=True),
        sa.Column('open_price', sa.Numeric, nullable=True),
        sa.Column('close_price', sa.Numeric, nullable=True),
        sa.Column('open_commission', sa.Numeric, nullable=True),
        sa.Column('close_commission', sa.Numeric, nullable=True),
        sa.Column('activation_time', sa.TIMESTAMP, nullable=True),
        sa.Column('open_time', sa.TIMESTAMP, nullable=True),
        sa.Column('close_time', sa.TIMESTAMP, nullable=True),
        sa.Column('close_reason', sa.String, nullable=True),
        sa.Column('start_updown_ticks', sa.Integer, nullable=True),
        sa.Column('trailing_stop_lose_ticks', sa.Integer, nullable=True),
        sa.Column('trailing_stop_win_ticks', sa.Integer, nullable=True),
        sa.Column('status', sa.String, nullable=True),
        sa.Column('exchange_status', sa.String, nullable=True),
        sa.Column('profit_loss', sa.Numeric, nullable=True),
        sa.Column('created_at', sa.TIMESTAMP, server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column('updated_at', sa.TIMESTAMP, server_default=sa.func.current_timestamp(), onupdate=sa.func.current_timestamp(), nullable=False)
    )

    op.create_index('idx_market_orders_created_at', 'market_orders', ['created_at'], unique=False)
    op.create_index('idx_market_orders_activation_time', 'market_orders', ['activation_time'], unique=False)
    op.create_index('idx_market_orders_open_time', 'market_orders', ['open_time'], unique=False)
    op.create_index('idx_market_orders_close_time', 'market_orders', ['close_time'], unique=False)
    op.create_index('idx_market_orders_profit_loss', 'market_orders', ['profit_loss'], unique=False)
    op.create_index('idx_market_orders_client_order_id', 'market_orders', ['client_order_id'], unique=False)
    op.create_index('idx_market_orders_exchange_order_id', 'market_orders', ['exchange_order_id'], unique=False)


def downgrade():
    """
    Откатывает изменения, удаляя таблицу market_orders и ее индексы.
    """
    op.drop_index('idx_market_orders_exchange_order_id', table_name='market_orders')
    op.drop_index('idx_market_orders_client_order_id', table_name='market_orders')
    op.drop_index('idx_market_orders_profit_loss', table_name='market_orders')
    op.drop_index('idx_market_orders_close_time', table_name='market_orders')
    op.drop_index('idx_market_orders_open_time', table_name='market_orders')
    op.drop_index('idx_market_orders_activation_time', table_name='market_orders')
    op.drop_index('idx_market_orders_created_at', table_name='market_orders')
    op.drop_table('market_orders')