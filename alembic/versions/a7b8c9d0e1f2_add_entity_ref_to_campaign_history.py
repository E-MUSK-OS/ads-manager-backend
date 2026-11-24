"""add entity_type, entity_id, recommendation_id to campaign_history

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-11-19 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("campaign_history", sa.Column("entity_type", sa.String(), nullable=True))
    op.add_column("campaign_history", sa.Column("entity_id", sa.Integer(), nullable=True))
    op.add_column(
        "campaign_history",
        sa.Column("recommendation_id", sa.Integer(), sa.ForeignKey("recommendations.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_campaign_history_entity", "campaign_history", ["entity_type", "entity_id"], unique=False)
    op.create_index("ix_campaign_history_recommendation_id", "campaign_history", ["recommendation_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_campaign_history_recommendation_id", table_name="campaign_history")
    op.drop_index("ix_campaign_history_entity", table_name="campaign_history")
    op.drop_column("campaign_history", "recommendation_id")
    op.drop_column("campaign_history", "entity_id")
    op.drop_column("campaign_history", "entity_type")
