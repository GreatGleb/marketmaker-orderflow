"""added assets_volume_volatility table

Revision ID: abff0bc26037
Revises: 10fa089f03df
Create Date: 2025-06-16 13:46:10.766975

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "abff0bc26037"
down_revision = "10fa089f03df"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "assets_volume_volatility",
        sa.Column(
            "exchange_pair_id",
            sa.Integer(),
            nullable=True,
            comment="Foreign key to exchange_pair_specs",
        ),
        sa.Column(
            "volume_24h_base",
            sa.Numeric(),
            nullable=False,
            comment="Base asset 24h volume",
        ),
        sa.Column(
            "volume_24h_quote",
            sa.Numeric(),
            nullable=False,
            comment="Quote asset 24h volume",
        ),
        sa.Column(
            "weighted_avg_price_24h",
            sa.Numeric(),
            nullable=False,
            comment="Weighted average price in 24h",
        ),
        sa.Column(
            "price_high", sa.Numeric(), nullable=True, comment="High 24h price"
        ),
        sa.Column(
            "price_low", sa.Numeric(), nullable=True, comment="Low 24h price"
        ),
        sa.Column(
            "volatility_percentage",
            sa.Numeric(),
            nullable=False,
            comment="Volatility percentage",
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
        sa.ForeignKeyConstraint(
            ["exchange_pair_id"],
            ["exchange_pair_specs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_assets_volume_volatility_created_at"),
        "assets_volume_volatility",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_assets_volume_volatility_exchange_pair_id"),
        "assets_volume_volatility",
        ["exchange_pair_id"],
        unique=False,
    )
    op.add_column(
        "assets_history",
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Date and time of create",
        ),
    )
    op.add_column(
        "assets_history",
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Date and time of update",
        ),
    )
    op.create_index(
        op.f("ix_assets_history_created_at"),
        "assets_history",
        ["created_at"],
        unique=False,
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(
        op.f("ix_assets_history_created_at"), table_name="assets_history"
    )
    op.drop_column("assets_history", "updated_at")
    op.drop_column("assets_history", "created_at")
    op.drop_index(
        op.f("ix_assets_volume_volatility_exchange_pair_id"),
        table_name="assets_volume_volatility",
    )
    op.drop_index(
        op.f("ix_assets_volume_volatility_created_at"),
        table_name="assets_volume_volatility",
    )
    op.drop_table("assets_volume_volatility")
    # ### end Alembic commands ###
