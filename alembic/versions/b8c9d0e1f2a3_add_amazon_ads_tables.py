"""add amazon_oauth_states, amazon_connections, and amazon_profiles tables

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-11-24 18:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    # 1. amazon_oauth_states
    if not insp.has_table("amazon_oauth_states"):
        op.create_table(
            "amazon_oauth_states",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("region", sa.String(), nullable=False, server_default="eu"),
            sa.Column("state_hash", sa.String(64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_amazon_oauth_states_id", "amazon_oauth_states", ["id"])
        op.create_index("ix_amazon_oauth_states_user_id", "amazon_oauth_states", ["user_id"])
        op.create_index("ix_amazon_oauth_states_state_hash", "amazon_oauth_states", ["state_hash"], unique=True)

    # 2. amazon_connections
    if not insp.has_table("amazon_connections"):
        op.create_table(
            "amazon_connections",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("region", sa.String(), nullable=False, server_default="eu"),
            sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="CONNECTED"),
            sa.Column("connected_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", "region", name="uq_amazon_connection_user_region"),
        )
        op.create_index("ix_amazon_connections_id", "amazon_connections", ["id"])
        op.create_index("ix_amazon_connections_user_id", "amazon_connections", ["user_id"])
        op.create_index("ix_amazon_connections_user_region", "amazon_connections", ["user_id", "region"])

    # 3. amazon_profiles
    if not insp.has_table("amazon_profiles"):
        op.create_table(
            "amazon_profiles",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("connection_id", sa.Integer(), nullable=False),
            sa.Column("profile_id", sa.String(), nullable=False),
            sa.Column("account_name", sa.String(), nullable=False),
            sa.Column("country_code", sa.String(), nullable=False),
            sa.Column("currency_code", sa.String(), nullable=False),
            sa.Column("timezone", sa.String(), nullable=False),
            sa.Column("is_selected", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.PrimaryKeyConstraint("id"),
            sa.ForeignKeyConstraint(["connection_id"], ["amazon_connections.id"], ondelete="CASCADE"),
            sa.UniqueConstraint("connection_id", "profile_id", name="uq_amazon_profile_connection_profile"),
        )
        op.create_index("ix_amazon_profiles_id", "amazon_profiles", ["id"])
        op.create_index("ix_amazon_profiles_connection_id", "amazon_profiles", ["connection_id"])
        op.create_index("ix_amazon_profiles_profile_id", "amazon_profiles", ["profile_id"])
        op.create_index("ix_amazon_profiles_conn_profile", "amazon_profiles", ["connection_id", "profile_id"])


def downgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)

    if insp.has_table("amazon_profiles"):
        op.drop_index("ix_amazon_profiles_conn_profile", table_name="amazon_profiles")
        op.drop_index("ix_amazon_profiles_profile_id", table_name="amazon_profiles")
        op.drop_index("ix_amazon_profiles_connection_id", table_name="amazon_profiles")
        op.drop_index("ix_amazon_profiles_id", table_name="amazon_profiles")
        op.drop_table("amazon_profiles")

    if insp.has_table("amazon_connections"):
        op.drop_index("ix_amazon_connections_user_region", table_name="amazon_connections")
        op.drop_index("ix_amazon_connections_user_id", table_name="amazon_connections")
        op.drop_index("ix_amazon_connections_id", table_name="amazon_connections")
        op.drop_table("amazon_connections")

    if insp.has_table("amazon_oauth_states"):
        op.drop_index("ix_amazon_oauth_states_state_hash", table_name="amazon_oauth_states")
        op.drop_index("ix_amazon_oauth_states_user_id", table_name="amazon_oauth_states")
        op.drop_index("ix_amazon_oauth_states_id", table_name="amazon_oauth_states")
        op.drop_table("amazon_oauth_states")
