"""Updated test order model

Revision ID: 867f5219f6a5
Revises: 5e39af31c315
Create Date: 2025-06-20 21:04:56.762189

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "867f5219f6a5"
down_revision = "5e39af31c315"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "test_orders",
        "open_time",
        existing_type=postgresql.TIMESTAMP(),
        type_=sa.DateTime(timezone=True),
        existing_comment="Time of order open",
        existing_nullable=False,
    )
    op.alter_column(
        "test_orders",
        "close_time",
        existing_type=postgresql.TIMESTAMP(),
        type_=sa.DateTime(timezone=True),
        existing_comment="Time of order close",
        existing_nullable=True,
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.alter_column(
        "test_orders",
        "close_time",
        existing_type=sa.DateTime(timezone=True),
        type_=postgresql.TIMESTAMP(),
        existing_comment="Time of order close",
        existing_nullable=True,
    )
    op.alter_column(
        "test_orders",
        "open_time",
        existing_type=sa.DateTime(timezone=True),
        type_=postgresql.TIMESTAMP(),
        existing_comment="Time of order open",
        existing_nullable=False,
    )
    # ### end Alembic commands ###
