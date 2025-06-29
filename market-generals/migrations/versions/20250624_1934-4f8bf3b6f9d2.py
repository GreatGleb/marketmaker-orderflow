"""Test bot table

Revision ID: 4f8bf3b6f9d2
Revises: 867f5219f6a5
Create Date: 2025-06-24 19:34:41.137368

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "4f8bf3b6f9d2"
down_revision = "867f5219f6a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "test_bots",
        sa.Column("symbol", sa.String(), nullable=False),
        sa.Column(
            "balance",
            sa.Numeric(precision=20, scale=10),
            nullable=False,
            comment="Balance for trading",
        ),
        sa.Column(
            "entry_offset_ticks",
            sa.Integer(),
            nullable=False,
            comment="Entry rejection in ticks (from best_bid/ask)",
        ),
        sa.Column(
            "exit_offset_ticks",
            sa.Integer(),
            nullable=False,
            comment="Target Profit/Close in Ticks",
        ),
        sa.Column(
            "stop_loss_ticks",
            sa.Integer(),
            nullable=False,
            comment="Stop-loss in ticks",
        ),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, comment="Is active bot"
        ),
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Date and time of create",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Date and time of update",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_test_bots_created_at"),
        "test_bots",
        ["created_at"],
        unique=False,
    )
    op.add_column(
        "test_orders", sa.Column("bot_id", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(None, "test_orders", "test_bots", ["bot_id"], ["id"])
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_constraint(None, "test_orders", type_="foreignkey")
    op.drop_column("test_orders", "bot_id")
    op.drop_index(op.f("ix_test_bots_created_at"), table_name="test_bots")
    op.drop_table("test_bots")
    # ### end Alembic commands ###
