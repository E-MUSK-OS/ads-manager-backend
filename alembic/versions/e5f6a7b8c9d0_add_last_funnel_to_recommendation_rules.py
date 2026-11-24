"""add last_funnel to recommendation_rules

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c0
Create Date: 2026-11-19 09:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5f6a7b8c9d0'
down_revision = 'd4e5f6a7b8c0'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'recommendation_rules',
        sa.Column('last_funnel', sa.JSON(), nullable=True)
    )


def downgrade():
    op.drop_column('recommendation_rules', 'last_funnel')
