"""add campaign_bid_thresholds table

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-11-18 10:40:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c3d4e5f6a7b9"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("campaign_bid_thresholds"):
        op.create_table(
            "campaign_bid_thresholds",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("campaign_id", sa.Integer(), nullable=False),
            sa.Column("min_bid", sa.Float(), nullable=True),
            sa.Column("max_bid", sa.Float(), nullable=True),
            sa.Column("target_acos", sa.Float(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_by", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["updated_by"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("campaign_id", name="uq_campaign_bid_thresholds_campaign_id"),
            sa.CheckConstraint("min_bid >= 0", name="ck_bid_threshold_min_bid"),
            sa.CheckConstraint("max_bid >= 0", name="ck_bid_threshold_max_bid"),
            sa.CheckConstraint("min_bid <= max_bid OR min_bid IS NULL OR max_bid IS NULL", name="ck_bid_threshold_min_le_max"),
            sa.CheckConstraint("target_acos >= 0 AND target_acos <= 100", name="ck_bid_threshold_target_acos"),
        )
        op.create_index(op.f("ix_campaign_bid_thresholds_id"), "campaign_bid_thresholds", ["id"], unique=False)
        op.create_index(op.f("ix_campaign_bid_thresholds_campaign_id"), "campaign_bid_thresholds", ["campaign_id"], unique=True)


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if insp.has_table("campaign_bid_thresholds"):
        op.drop_index(op.f("ix_campaign_bid_thresholds_campaign_id"), table_name="campaign_bid_thresholds")
        op.drop_index(op.f("ix_campaign_bid_thresholds_id"), table_name="campaign_bid_thresholds")
        op.drop_table("campaign_bid_thresholds")
