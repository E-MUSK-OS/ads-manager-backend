"""recommendations module schema

Revision ID: d7f2a1b9e034
Revises: c8e4a1b2d9f0
Create Date: 2026-10-21 17:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d7f2a1b9e034"
down_revision: Union[str, Sequence[str], None] = "c8e4a1b2d9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Alter search_terms to add orders column with backfill
    with op.batch_alter_table("search_terms") as batch_op:
        batch_op.add_column(
            sa.Column("orders", sa.Integer(), nullable=False, server_default="0")
        )

    # Backfill approximation: existing search_terms with sales > 0 assume at least 1 order, else 0
    op.execute(sa.text("UPDATE search_terms SET orders = CASE WHEN sales > 0 THEN 1 ELSE 0 END"))

    # 2. recommendation_rules table
    op.create_table(
        "recommendation_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ads_account_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=60), nullable=False),
        sa.Column("name_key", sa.String(length=60), nullable=False),
        sa.Column("affected_parameter", sa.String(), nullable=False),
        sa.Column("date_filter", sa.String(), nullable=False),
        sa.Column("min_clicks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("actions", sa.JSON(), nullable=False),
        sa.Column("scope_mode", sa.String(), nullable=False, server_default="ALL"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("template_key", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_match_count", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["ads_account_id"], ["ads_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ads_account_id", "name_key", name="uq_recommendation_rules_account_name_key"),
        sa.CheckConstraint(
            "affected_parameter IN ('CAMPAIGN', 'PLACEMENT', 'TARGETING', 'SEARCH_TERM')",
            name="ck_rec_rules_affected_parameter",
        ),
        sa.CheckConstraint("scope_mode IN ('ALL', 'INCLUDE', 'EXCLUDE')", name="ck_rec_rules_scope_mode"),
    )
    op.create_index(op.f("ix_recommendation_rules_id"), "recommendation_rules", ["id"], unique=False)
    op.create_index(op.f("ix_recommendation_rules_ads_account_id"), "recommendation_rules", ["ads_account_id"], unique=False)

    # 3. recommendation_rule_campaigns table
    op.create_table(
        "recommendation_rule_campaigns",
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rule_id"], ["recommendation_rules.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("rule_id", "campaign_id"),
    )

    # 4. recommendations table
    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ads_account_id", sa.Integer(), nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=True),
        sa.Column("rule_name", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="RULE"),
        sa.Column("affected_parameter", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("entity_label", sa.String(), nullable=False),
        sa.Column("campaign_id", sa.Integer(), nullable=False),
        sa.Column("ad_group_id", sa.Integer(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("suggested_actions", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="OPEN"),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("apply_result", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["ad_group_id"], ["ad_groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ads_account_id"], ["ads_accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["recommendation_rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('OPEN', 'APPLIED', 'DISMISSED', 'EXPIRED', 'FAILED')",
            name="ck_recommendations_status",
        ),
    )
    op.create_index(op.f("ix_recommendations_id"), "recommendations", ["id"], unique=False)
    op.create_index(op.f("ix_recommendations_ads_account_id"), "recommendations", ["ads_account_id"], unique=False)
    op.create_index(op.f("ix_recommendations_campaign_id"), "recommendations", ["campaign_id"], unique=False)
    op.create_index(op.f("ix_recommendations_dedupe_key"), "recommendations", ["dedupe_key"], unique=False)
    op.create_index("ix_recommendations_account_status", "recommendations", ["ads_account_id", "status"], unique=False)
    op.create_index("ix_recommendations_account_dedupe", "recommendations", ["ads_account_id", "dedupe_key"], unique=False)

    # 5. recommendation_runs table
    op.create_table(
        "recommendation_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ads_account_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(), nullable=False),
        sa.Column("rules_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expired", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="SUCCESS"),
        sa.ForeignKeyConstraint(["ads_account_id"], ["ads_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_recommendation_runs_id"), "recommendation_runs", ["id"], unique=False)
    op.create_index(op.f("ix_recommendation_runs_ads_account_id"), "recommendation_runs", ["ads_account_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_recommendation_runs_ads_account_id"), table_name="recommendation_runs")
    op.drop_index(op.f("ix_recommendation_runs_id"), table_name="recommendation_runs")
    op.drop_table("recommendation_runs")

    op.drop_index("ix_recommendations_account_dedupe", table_name="recommendations")
    op.drop_index("ix_recommendations_account_status", table_name="recommendations")
    op.drop_index(op.f("ix_recommendations_dedupe_key"), table_name="recommendations")
    op.drop_index(op.f("ix_recommendations_campaign_id"), table_name="recommendations")
    op.drop_index(op.f("ix_recommendations_ads_account_id"), table_name="recommendations")
    op.drop_index(op.f("ix_recommendations_id"), table_name="recommendations")
    op.drop_table("recommendations")

    op.drop_table("recommendation_rule_campaigns")

    op.drop_index(op.f("ix_recommendation_rules_ads_account_id"), table_name="recommendation_rules")
    op.drop_index(op.f("ix_recommendation_rules_id"), table_name="recommendation_rules")
    op.drop_table("recommendation_rules")

    with op.batch_alter_table("search_terms") as batch_op:
        batch_op.drop_column("orders")
