"""add placement_metrics_daily table

Revision ID: e2f3a4b5c6d7
Revises: d7f2a1b9e034
Create Date: 2026-10-21 18:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, Sequence[str], None] = "d7f2a1b9e034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "placement_metrics_daily",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("placement", sa.String(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("impressions", sa.Integer(), server_default="0", nullable=False),
        sa.Column("clicks", sa.Integer(), server_default="0", nullable=False),
        sa.Column("spend", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("sales", sa.Float(), server_default="0.0", nullable=False),
        sa.Column("orders", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "placement IN ('TOP_OF_SEARCH', 'REST_OF_SEARCH', 'PRODUCT_PAGES')",
            name="ck_placement_metrics_daily_placement",
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"],
            ["campaigns.id"],
            ondelete="CASCADE",
            name="fk_placement_metrics_daily_campaign_id",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_placement_metrics_daily"),
        sa.UniqueConstraint("campaign_id", "placement", "date", name="uix_placement_metrics_daily"),
    )
    op.create_index(
        "ix_placement_metrics_daily_campaign_date",
        "placement_metrics_daily",
        ["campaign_id", "date"],
        unique=False,
    )
    op.create_index(
        "ix_placement_metrics_daily_campaign_id",
        "placement_metrics_daily",
        ["campaign_id"],
        unique=False,
    )
    op.create_index(
        "ix_placement_metrics_daily_date",
        "placement_metrics_daily",
        ["date"],
        unique=False,
    )
    op.create_index(
        "ix_placement_metrics_daily_id",
        "placement_metrics_daily",
        ["id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("placement_metrics_daily")
