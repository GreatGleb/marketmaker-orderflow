"""Add pg_repack extension

Revision ID: ff34a181d532
Revises: 4b13091d66c9
Create Date: 2025-07-27 13:22:03.484603

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ff34a181d532'
down_revision = '4b13091d66c9'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_repack;")
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.execute("DROP EXTENSION IF EXISTS pg_repack;")
    # ### end Alembic commands ###
