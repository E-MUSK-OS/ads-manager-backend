"""Add Product and ProductReview models

Revision ID: 3d3ce651a479
Revises: 9067b640ca3d
Create Date: 2026-10-05 17:06:24.433012

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3d3ce651a479'
down_revision: Union[str, Sequence[str], None] = '9067b640ca3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if not insp.has_table("products"):
        op.create_table(
            "products",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("ads_account_id", sa.Integer(), nullable=False),
            sa.Column("asin", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("image_url", sa.String(), nullable=True),
            sa.Column("category", sa.String(), nullable=True),
            sa.Column("price", sa.Float(), nullable=False),
            sa.Column("rating_avg", sa.Float(), nullable=True, server_default="0.0"),
            sa.Column("review_count", sa.Integer(), nullable=True, server_default="0"),
            sa.ForeignKeyConstraint(["ads_account_id"], ["ads_accounts.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("ads_account_id", "asin", name="uix_product_ads_account_asin"),
        )
        op.create_index(op.f("ix_products_id"), "products", ["id"], unique=False)
        op.create_index(op.f("ix_products_asin"), "products", ["asin"], unique=False)
        op.create_index(op.f("ix_products_ads_account_id"), "products", ["ads_account_id"], unique=False)

    if not insp.has_table("product_reviews"):
        op.create_table(
            "product_reviews",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("product_id", sa.Integer(), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column("review_text", sa.Text(), nullable=True),
            sa.Column("review_date", sa.Date(), nullable=False),
            sa.Column("verified_purchase", sa.Boolean(), nullable=True, server_default=sa.true()),
            sa.CheckConstraint("rating >= 1 AND rating <= 5", name="check_rating_range"),
            sa.ForeignKeyConstraint(["product_id"], ["products.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(op.f("ix_product_reviews_id"), "product_reviews", ["id"], unique=False)
        op.create_index(op.f("ix_product_reviews_product_id"), "product_reviews", ["product_id"], unique=False)
        op.create_index(op.f("ix_product_reviews_review_date"), "product_reviews", ["review_date"], unique=False)

    insp = sa.inspect(bind)
    campaign_cols = {c["name"] for c in insp.get_columns("campaigns")}
    if "product_id" not in campaign_cols:
        op.add_column("campaigns", sa.Column("product_id", sa.Integer(), nullable=True))
        op.create_index(op.f("ix_campaigns_product_id"), "campaigns", ["product_id"], unique=False)

    fks = {fk.get("name") for fk in insp.get_foreign_keys("campaigns")}
    if "campaigns_product_id_fkey" not in fks:
        op.create_foreign_key(
            "campaigns_product_id_fkey",
            "campaigns",
            "products",
            ["product_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    fks = {fk.get("name") for fk in insp.get_foreign_keys("campaigns")}
    if "campaigns_product_id_fkey" in fks:
        op.drop_constraint("campaigns_product_id_fkey", "campaigns", type_="foreignkey")
    campaign_cols = {c["name"] for c in insp.get_columns("campaigns")}
    if "product_id" in campaign_cols:
        op.drop_index(op.f("ix_campaigns_product_id"), table_name="campaigns")
        op.drop_column("campaigns", "product_id")
    if insp.has_table("product_reviews"):
        op.drop_index(op.f("ix_product_reviews_review_date"), table_name="product_reviews")
        op.drop_index(op.f("ix_product_reviews_product_id"), table_name="product_reviews")
        op.drop_index(op.f("ix_product_reviews_id"), table_name="product_reviews")
        op.drop_table("product_reviews")
    if insp.has_table("products"):
        op.drop_index(op.f("ix_products_ads_account_id"), table_name="products")
        op.drop_index(op.f("ix_products_asin"), table_name="products")
        op.drop_index(op.f("ix_products_id"), table_name="products")
        op.drop_table("products")
