"""add campaign_history table

Revision ID: b2c3d4e5f6a7
Revises: f3a4b5c6d7e8
Create Date: 2026-11-05 13:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "campaign_history",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("event_title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("previous_value", sa.String(), nullable=True),
        sa.Column("new_value", sa.String(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_campaign_history_id", "campaign_history", ["id"], unique=False)
    op.create_index("ix_campaign_history_campaign_id", "campaign_history", ["campaign_id"], unique=False)
    op.create_index("ix_campaign_history_event_type", "campaign_history", ["event_type"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_campaign_history_event_type", table_name="campaign_history")
    op.drop_index("ix_campaign_history_campaign_id", table_name="campaign_history")
    op.drop_index("ix_campaign_history_id", table_name="campaign_history")
    op.drop_table("campaign_history")

