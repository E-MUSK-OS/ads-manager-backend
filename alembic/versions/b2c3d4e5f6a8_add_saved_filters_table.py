"""add saved_filters table

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f6
Create Date: 2026-11-18 10:15:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a8"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "saved_filters",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_saved_filters_id"), "saved_filters", ["id"], unique=False)
    op.create_index(op.f("ix_saved_filters_user_id"), "saved_filters", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_saved_filters_user_id"), table_name="saved_filters")
    op.drop_index(op.f("ix_saved_filters_id"), table_name="saved_filters")
    op.drop_table("saved_filters")
