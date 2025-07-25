"""Add stop_win_percents, stop_loss_percents, start_updown_percents to test_bots

Revision ID: 9d9169de41f2
Revises: 463fec2d1de4
Create Date: 2025-07-22 14:49:01.156164

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '9d9169de41f2'
down_revision = '463fec2d1de4'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f('idx_market_orders_activation_time'), table_name='market_orders')
    op.drop_index(op.f('idx_market_orders_client_order_id'), table_name='market_orders')
    op.drop_index(op.f('idx_market_orders_close_time'), table_name='market_orders')
    op.drop_index(op.f('idx_market_orders_created_at'), table_name='market_orders')
    op.drop_index(op.f('idx_market_orders_exchange_order_id'), table_name='market_orders')
    op.drop_index(op.f('idx_market_orders_open_time'), table_name='market_orders')
    op.drop_index(op.f('idx_market_orders_profit_loss'), table_name='market_orders')
    op.create_index(op.f('ix_market_orders_activation_time'), 'market_orders', ['activation_time'], unique=False)
    op.create_index(op.f('ix_market_orders_client_order_id'), 'market_orders', ['client_order_id'], unique=False)
    op.create_index(op.f('ix_market_orders_close_time'), 'market_orders', ['close_time'], unique=False)
    op.create_index(op.f('ix_market_orders_created_at'), 'market_orders', ['created_at'], unique=False)
    op.create_index(op.f('ix_market_orders_exchange_order_id'), 'market_orders', ['exchange_order_id'], unique=False)
    op.create_index(op.f('ix_market_orders_open_time'), 'market_orders', ['open_time'], unique=False)
    op.create_index(op.f('ix_market_orders_profit_loss'), 'market_orders', ['profit_loss'], unique=False)
    op.add_column('test_bots', sa.Column('stop_win_percents', sa.Numeric(), nullable=True, comment='Stop win percentage'))
    op.add_column('test_bots', sa.Column('stop_loss_percents', sa.Numeric(), nullable=True, comment='Stop loss percentage'))
    op.add_column('test_bots', sa.Column('start_updown_percents', sa.Numeric(), nullable=True, comment='Start up/down percentage'))

    op.alter_column('test_bots', 'stop_success_ticks', existing_type=sa.Integer(), nullable=True)
    op.alter_column('test_bots', 'stop_loss_ticks', existing_type=sa.Integer(), nullable=True)
    op.alter_column('test_bots', 'start_updown_ticks', existing_type=sa.Integer(), nullable=True)
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column('test_bots', 'start_updown_percents')
    op.drop_column('test_bots', 'stop_loss_percents')
    op.drop_column('test_bots', 'stop_win_percents')
    op.drop_index(op.f('ix_market_orders_profit_loss'), table_name='market_orders')
    op.drop_index(op.f('ix_market_orders_open_time'), table_name='market_orders')
    op.drop_index(op.f('ix_market_orders_exchange_order_id'), table_name='market_orders')
    op.drop_index(op.f('ix_market_orders_created_at'), table_name='market_orders')
    op.drop_index(op.f('ix_market_orders_close_time'), table_name='market_orders')
    op.drop_index(op.f('ix_market_orders_client_order_id'), table_name='market_orders')
    op.drop_index(op.f('ix_market_orders_activation_time'), table_name='market_orders')
    op.create_index(op.f('idx_market_orders_profit_loss'), 'market_orders', ['profit_loss'], unique=False)
    op.create_index(op.f('idx_market_orders_open_time'), 'market_orders', ['open_time'], unique=False)
    op.create_index(op.f('idx_market_orders_exchange_order_id'), 'market_orders', ['exchange_order_id'], unique=False)
    op.create_index(op.f('idx_market_orders_created_at'), 'market_orders', ['created_at'], unique=False)
    op.create_index(op.f('idx_market_orders_close_time'), 'market_orders', ['close_time'], unique=False)
    op.create_index(op.f('idx_market_orders_client_order_id'), 'market_orders', ['client_order_id'], unique=False)
    op.create_index(op.f('idx_market_orders_activation_time'), 'market_orders', ['activation_time'], unique=False)
    # ### end Alembic commands ###
