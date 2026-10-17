"""campaign site, drafts, negative brand target kind, product sku, user health rules

Revision ID: c8e4a1b2d9f0
Revises: db4a2108e7f8
Create Date: 2026-10-16 10:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8e4a1b2d9f0"
down_revision: Union[str, Sequence[str], None] = "db4a2108e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    with op.batch_alter_table("campaigns") as batch:
        # Mock attribute; mapping to the real Amazon Ads API is unverified.
        batch.add_column(
            sa.Column("site", sa.String(), nullable=False, server_default="AMAZON_AND_BEYOND")
        )
        batch.create_check_constraint(
            "ck_campaign_site",
            "site IN ('AMAZON_AND_BEYOND', 'AMAZON_BUSINESS')",
        )

    with op.batch_alter_table("targets") as batch:
        batch.drop_constraint("ck_target_kind", type_="check")
        batch.create_check_constraint(
            "ck_target_kind",
            "kind IN ('AUTO_GROUP', 'ASIN', 'CATEGORY', 'NEGATIVE_ASIN', 'NEGATIVE_BRAND')",
        )

    if not insp.has_table("campaign_drafts"):
        op.create_table(
            "campaign_drafts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("ads_account_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("step", sa.Integer(), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["ads_account_id"], ["ads_accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_campaign_drafts_id"), "campaign_drafts", ["id"], unique=False)
        op.create_index(op.f("ix_campaign_drafts_user_id"), "campaign_drafts", ["user_id"], unique=False)
        op.create_index(op.f("ix_campaign_drafts_ads_account_id"), "campaign_drafts", ["ads_account_id"], unique=False)
        op.create_index("ix_campaign_drafts_user_account", "campaign_drafts", ["user_id", "ads_account_id"], unique=False)

    if insp.has_table("products"):
        cols = [c["name"] for c in insp.get_columns("products")]
        if "sku" not in cols:
            with op.batch_alter_table("products") as batch:
                batch.add_column(sa.Column("sku", sa.String(), nullable=True))
                batch.create_index(op.f("ix_products_sku"), ["sku"], unique=False)

    if insp.has_table("user_campaign_settings"):
        cols = [c["name"] for c in insp.get_columns("user_campaign_settings")]
        if "health_rules" not in cols:
            with op.batch_alter_table("user_campaign_settings") as batch:
                batch.add_column(sa.Column("health_rules", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if insp.has_table("user_campaign_settings"):
        cols = [c["name"] for c in insp.get_columns("user_campaign_settings")]
        if "health_rules" in cols:
            with op.batch_alter_table("user_campaign_settings") as batch:
                batch.drop_column("health_rules")

    if insp.has_table("products"):
        cols = [c["name"] for c in insp.get_columns("products")]
        if "sku" in cols:
            with op.batch_alter_table("products") as batch:
                batch.drop_index(op.f("ix_products_sku"))
                batch.drop_column("sku")

    if insp.has_table("campaign_drafts"):
        op.drop_index("ix_campaign_drafts_user_account", table_name="campaign_drafts")
        op.drop_index(op.f("ix_campaign_drafts_ads_account_id"), table_name="campaign_drafts")
        op.drop_index(op.f("ix_campaign_drafts_user_id"), table_name="campaign_drafts")
        op.drop_index(op.f("ix_campaign_drafts_id"), table_name="campaign_drafts")
        op.drop_table("campaign_drafts")

    with op.batch_alter_table("targets") as batch:
        batch.drop_constraint("ck_target_kind", type_="check")
        batch.create_check_constraint(
            "ck_target_kind",
            "kind IN ('AUTO_GROUP', 'ASIN', 'CATEGORY', 'NEGATIVE_ASIN')",
        )

    with op.batch_alter_table("campaigns") as batch:
        batch.drop_constraint("ck_campaign_site", type_="check")
        batch.drop_column("site")
