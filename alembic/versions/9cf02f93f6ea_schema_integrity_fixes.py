"""schema integrity fixes

Revision ID: 9cf02f93f6ea
Revises: 3b6029a61e44
Create Date: 2026-10-03 10:59:36.220028

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9cf02f93f6ea'
down_revision: Union[str, Sequence[str], None] = '3b6029a61e44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. metrics_daily constraints
    op.create_check_constraint('check_entity_type', 'metrics_daily', "entity_type IN ('campaign', 'ad_group', 'keyword')")
    op.create_unique_constraint('uix_metric_daily', 'metrics_daily', ['entity_type', 'entity_id', 'date'])
    
    # 2. campaigns composite unique constraint
    op.create_unique_constraint('uix_campaign_external_id', 'campaigns', ['ads_account_id', 'external_id'])
    
    # 3. Add explicit indexes on FK columns
    op.create_index(op.f('ix_ads_accounts_user_id'), 'ads_accounts', ['user_id'])
    op.create_index(op.f('ix_campaigns_ads_account_id'), 'campaigns', ['ads_account_id'])
    op.create_index(op.f('ix_ad_groups_campaign_id'), 'ad_groups', ['campaign_id'])
    op.create_index(op.f('ix_keywords_ad_group_id'), 'keywords', ['ad_group_id'])
    op.create_index(op.f('ix_search_terms_campaign_id'), 'search_terms', ['campaign_id'])
    op.create_index(op.f('ix_search_terms_ad_group_id'), 'search_terms', ['ad_group_id'])
    op.create_index(op.f('ix_search_terms_keyword_id'), 'search_terms', ['keyword_id'])
    op.create_index(op.f('ix_automation_rules_ads_account_id'), 'automation_rules', ['ads_account_id'])
    op.create_index(op.f('ix_ai_suggestions_ads_account_id'), 'ai_suggestions', ['ads_account_id'])
    op.create_index(op.f('ix_ai_chat_messages_user_id'), 'ai_chat_messages', ['user_id'])

    # 4. Modify foreign keys to CASCADE
    op.drop_constraint('ads_accounts_user_id_fkey', 'ads_accounts', type_='foreignkey')
    op.create_foreign_key('ads_accounts_user_id_fkey', 'ads_accounts', 'users', ['user_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('campaigns_ads_account_id_fkey', 'campaigns', type_='foreignkey')
    op.create_foreign_key('campaigns_ads_account_id_fkey', 'campaigns', 'ads_accounts', ['ads_account_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('ad_groups_campaign_id_fkey', 'ad_groups', type_='foreignkey')
    op.create_foreign_key('ad_groups_campaign_id_fkey', 'ad_groups', 'campaigns', ['campaign_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('keywords_ad_group_id_fkey', 'keywords', type_='foreignkey')
    op.create_foreign_key('keywords_ad_group_id_fkey', 'keywords', 'ad_groups', ['ad_group_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('search_terms_campaign_id_fkey', 'search_terms', type_='foreignkey')
    op.create_foreign_key('search_terms_campaign_id_fkey', 'search_terms', 'campaigns', ['campaign_id'], ['id'], ondelete='CASCADE')
    
    op.drop_constraint('search_terms_ad_group_id_fkey', 'search_terms', type_='foreignkey')
    op.create_foreign_key('search_terms_ad_group_id_fkey', 'search_terms', 'ad_groups', ['ad_group_id'], ['id'], ondelete='CASCADE')
    
    op.drop_constraint('search_terms_keyword_id_fkey', 'search_terms', type_='foreignkey')
    op.create_foreign_key('search_terms_keyword_id_fkey', 'search_terms', 'keywords', ['keyword_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('automation_rules_ads_account_id_fkey', 'automation_rules', type_='foreignkey')
    op.create_foreign_key('automation_rules_ads_account_id_fkey', 'automation_rules', 'ads_accounts', ['ads_account_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('ai_suggestions_ads_account_id_fkey', 'ai_suggestions', type_='foreignkey')
    op.create_foreign_key('ai_suggestions_ads_account_id_fkey', 'ai_suggestions', 'ads_accounts', ['ads_account_id'], ['id'], ondelete='CASCADE')

    op.drop_constraint('ai_chat_messages_user_id_fkey', 'ai_chat_messages', type_='foreignkey')
    op.create_foreign_key('ai_chat_messages_user_id_fkey', 'ai_chat_messages', 'users', ['user_id'], ['id'], ondelete='CASCADE')


def downgrade() -> None:
    """Downgrade schema."""
    pass
