"""add campaign_status_daily table

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-11-19 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f6a7b8c9d0e1'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'campaign_status_daily',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ads_account_id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('state', sa.String(length=30), nullable=False),
        sa.Column('serving_status', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['ads_account_id'], ['ads_accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ads_account_id', 'campaign_id', 'date', name='uq_campaign_status_daily_account_camp_date')
    )
    op.create_index(op.f('ix_campaign_status_daily_ads_account_id'), 'campaign_status_daily', ['ads_account_id'], unique=False)
    op.create_index(op.f('ix_campaign_status_daily_campaign_id'), 'campaign_status_daily', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_campaign_status_daily_date'), 'campaign_status_daily', ['date'], unique=False)
    op.create_index(op.f('ix_campaign_status_daily_id'), 'campaign_status_daily', ['id'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_campaign_status_daily_id'), table_name='campaign_status_daily')
    op.drop_index(op.f('ix_campaign_status_daily_date'), table_name='campaign_status_daily')
    op.drop_index(op.f('ix_campaign_status_daily_campaign_id'), table_name='campaign_status_daily')
    op.drop_index(op.f('ix_campaign_status_daily_ads_account_id'), table_name='campaign_status_daily')
    op.drop_table('campaign_status_daily')
