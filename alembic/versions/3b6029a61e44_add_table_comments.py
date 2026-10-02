"""add table comments

Revision ID: 3b6029a61e44
Revises: 47f7bf985e55
Create Date: 2026-10-02 18:36:03.138446

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3b6029a61e44'
down_revision: Union[str, Sequence[str], None] = '47f7bf985e55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("COMMENT ON TABLE users IS 'User accounts and authentication data';")
    op.execute("COMMENT ON TABLE ads_accounts IS 'One row per connected Amazon Ads account, linked to a user';")
    op.execute("COMMENT ON COLUMN ads_accounts.profile_id IS 'The external ID from Amazon Ads provider';")
    op.execute("COMMENT ON TABLE campaigns IS 'Advertising campaigns synchronized from provider';")
    op.execute("COMMENT ON TABLE ad_groups IS 'Ad groups within a campaign';")
    op.execute("COMMENT ON TABLE keywords IS 'Keywords targeted by ad groups';")
    op.execute("COMMENT ON TABLE search_terms IS 'Actual search terms that triggered ads';")
    op.execute("COMMENT ON TABLE metrics_daily IS 'Daily performance metrics for campaigns/ad groups';")
    op.execute("COMMENT ON TABLE automation_rules IS 'User-defined rules for bid and budget automation';")
    op.execute("COMMENT ON TABLE ai_suggestions IS 'AI-generated optimization suggestions';")
    op.execute("COMMENT ON TABLE ai_chat_messages IS 'History of AI chat interactions for the user';")

def downgrade() -> None:
    """Downgrade schema."""
    op.execute("COMMENT ON TABLE users IS NULL;")
    op.execute("COMMENT ON TABLE ads_accounts IS NULL;")
    op.execute("COMMENT ON COLUMN ads_accounts.profile_id IS NULL;")
    op.execute("COMMENT ON TABLE campaigns IS NULL;")
    op.execute("COMMENT ON TABLE ad_groups IS NULL;")
    op.execute("COMMENT ON TABLE keywords IS NULL;")
    op.execute("COMMENT ON TABLE search_terms IS NULL;")
    op.execute("COMMENT ON TABLE metrics_daily IS NULL;")
    op.execute("COMMENT ON TABLE automation_rules IS NULL;")
    op.execute("COMMENT ON TABLE ai_suggestions IS NULL;")
    op.execute("COMMENT ON TABLE ai_chat_messages IS NULL;")
