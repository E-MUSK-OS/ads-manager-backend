"""add hourly stream and metrics tables

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b9
Create Date: 2026-11-18 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e5f6a7b8c0'
down_revision = 'c3d4e5f6a7b9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'hourly_stream_status',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ads_account_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(), nullable=False, server_default='NOT_CONNECTED'),
        sa.Column('sp_traffic_subscription_id', sa.String(), nullable=True),
        sa.Column('sp_conversion_subscription_id', sa.String(), nullable=True),
        sa.Column('destination_arn', sa.String(), nullable=True),
        sa.Column('subscribed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['ads_account_id'], ['ads_accounts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ads_account_id', name='uq_hourly_stream_status_account')
    )
    op.create_index(op.f('ix_hourly_stream_status_id'), 'hourly_stream_status', ['id'], unique=False)
    op.create_index(op.f('ix_hourly_stream_status_ads_account_id'), 'hourly_stream_status', ['ads_account_id'], unique=True)

    op.create_table(
        'campaign_hourly_metrics',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('ads_account_id', sa.Integer(), nullable=False),
        sa.Column('campaign_id', sa.Integer(), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('hour', sa.Integer(), nullable=False),
        sa.Column('impressions', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('clicks', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('cost', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('purchases', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('sales', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(['ads_account_id'], ['ads_accounts.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['campaign_id'], ['campaigns.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('ads_account_id', 'campaign_id', 'date', 'hour', name='uq_campaign_hourly_metric')
    )
    op.create_index(op.f('ix_campaign_hourly_metrics_id'), 'campaign_hourly_metrics', ['id'], unique=False)
    op.create_index(op.f('ix_campaign_hourly_metrics_ads_account_id'), 'campaign_hourly_metrics', ['ads_account_id'], unique=False)
    op.create_index(op.f('ix_campaign_hourly_metrics_campaign_id'), 'campaign_hourly_metrics', ['campaign_id'], unique=False)
    op.create_index(op.f('ix_campaign_hourly_metrics_date'), 'campaign_hourly_metrics', ['date'], unique=False)


def downgrade():
    op.drop_index(op.f('ix_campaign_hourly_metrics_date'), table_name='campaign_hourly_metrics')
    op.drop_index(op.f('ix_campaign_hourly_metrics_campaign_id'), table_name='campaign_hourly_metrics')
    op.drop_index(op.f('ix_campaign_hourly_metrics_ads_account_id'), table_name='campaign_hourly_metrics')
    op.drop_index(op.f('ix_campaign_hourly_metrics_id'), table_name='campaign_hourly_metrics')
    op.drop_table('campaign_hourly_metrics')

    op.drop_index(op.f('ix_hourly_stream_status_ads_account_id'), table_name='hourly_stream_status')
    op.drop_index(op.f('ix_hourly_stream_status_id'), table_name='hourly_stream_status')
    op.drop_table('hourly_stream_status')
