"""Update with time zone

Revision ID: d526351d0c0f
Revises: 07113a535263
Create Date: 2025-06-18 16:26:24.899702

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d526351d0c0f"
down_revision = "07113a535263"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "asset_history", "event_time", type_=sa.TIMESTAMP(timezone=True)
    )


def downgrade():
    op.alter_column(
        "asset_history", "event_time", type_=sa.TIMESTAMP(timezone=False)
    )
