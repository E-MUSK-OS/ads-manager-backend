"""add sync_runs table and sync_status column

Revision ID: a1b2c3d4e5f6
Revises: f5a6b7c8d9e0
Create Date: 2026-11-18 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "ads_accounts",
        sa.Column("sync_status", sa.String(), server_default="idle", nullable=False)
    )
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ads_account_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), server_default="running", nullable=False),
        sa.Column("sync_type", sa.String(), server_default="full", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("campaigns_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("metrics_rows_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("date_range_start", sa.Date(), nullable=True),
        sa.Column("date_range_end", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["ads_account_id"], ["ads_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sync_runs_ads_account_id"), "sync_runs", ["ads_account_id"], unique=False)
    op.create_index(op.f("ix_sync_runs_id"), "sync_runs", ["id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sync_runs_id"), table_name="sync_runs")
    op.drop_index(op.f("ix_sync_runs_ads_account_id"), table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_column("ads_accounts", "sync_status")
