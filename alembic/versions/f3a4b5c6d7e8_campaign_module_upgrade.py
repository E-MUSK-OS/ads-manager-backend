"""campaign module upgrade: indian marketplace rules and schema integrity

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-11-05 12:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("campaigns") as batch:
        batch.add_column(sa.Column("amazon_campaign_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("delivery_status", sa.String(), nullable=False, server_default="DELIVERING"))
        batch.add_column(sa.Column("placement_top_of_search_pct", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("placement_product_pages_pct", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("placement_rest_of_search_pct", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("portfolio_name", sa.String(), nullable=True))
        batch.add_column(sa.Column("negative_brands", sa.JSON(), nullable=False, server_default="[]"))
        batch.create_index("ix_campaigns_amazon_campaign_id", ["amazon_campaign_id"], unique=False)

    with op.batch_alter_table("targets") as batch:
        batch.add_column(sa.Column("campaign_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("target_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("expression_type", sa.String(), nullable=True))
        batch.add_column(sa.Column("expression_value", sa.String(), nullable=True))
        batch.create_foreign_key("fk_targets_campaign_id", "campaigns", ["campaign_id"], ["id"], ondelete="CASCADE")
        batch.create_index("ix_targets_campaign_id", ["campaign_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("targets") as batch:
        batch.drop_index("ix_targets_campaign_id")
        batch.drop_constraint("fk_targets_campaign_id", type_="foreignkey")
        batch.drop_column("expression_value")
        batch.drop_column("expression_type")
        batch.drop_column("target_type")
        batch.drop_column("campaign_id")

    with op.batch_alter_table("campaigns") as batch:
        batch.drop_index("ix_campaigns_amazon_campaign_id")
        batch.drop_column("negative_brands")
        batch.drop_column("portfolio_name")
        batch.drop_column("placement_rest_of_search_pct")
        batch.drop_column("placement_product_pages_pct")
        batch.drop_column("placement_top_of_search_pct")
        batch.drop_column("delivery_status")
        batch.drop_column("amazon_campaign_id")

