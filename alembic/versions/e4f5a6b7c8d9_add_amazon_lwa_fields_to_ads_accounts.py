"""add amazon lwa fields to ads_accounts

Revision ID: e4f5a6b7c8d9
Revises: b2c3d4e5f6a7
Create Date: 2026-11-16 10:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e4f5a6b7c8d9"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ads_accounts", sa.Column("refresh_token_encrypted", sa.Text(), nullable=True))
    op.add_column("ads_accounts", sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ads_accounts", sa.Column("region", sa.String(), nullable=True))
    op.add_column("ads_accounts", sa.Column("amazon_account_name", sa.String(), nullable=True))
    op.add_column("ads_accounts", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "ads_accounts",
        sa.Column("connection_status", sa.String(), server_default="connected", nullable=False)
    )


def downgrade() -> None:
    op.drop_column("ads_accounts", "connection_status")
    op.drop_column("ads_accounts", "last_synced_at")
    op.drop_column("ads_accounts", "amazon_account_name")
    op.drop_column("ads_accounts", "region")
    op.drop_column("ads_accounts", "access_token_expires_at")
    op.drop_column("ads_accounts", "refresh_token_encrypted")

