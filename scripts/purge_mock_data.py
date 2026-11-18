"""
Purge Mock & Duplicate Ads Accounts Script

Identifies and optionally deletes mock and duplicate accounts created during testing.
Default is DRY RUN (no data is modified).

Usage:
  Dry-run: python -m scripts.purge_mock_data
  Execute: python -m scripts.purge_mock_data --execute
"""

import sys
import asyncio
import argparse
from sqlalchemy import select, func, delete, or_
from app.database import AsyncSessionLocal
from app.models.user import User
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.search_term import SearchTerm
from app.models.negative_keyword import NegativeKeyword
from app.models.campaign_placement import CampaignPlacement
from app.models.campaign_history import CampaignHistory
from app.models.recommendation import Recommendation, RecommendationEvent
from app.models.campaign_draft import CampaignDraft


async def inspect_and_purge(execute: bool = False):
    print("=" * 60)
    print("AdsManager - Purge Mock & Duplicate Ads Accounts")
    print(f"Mode: {'EXECUTE (Permanent deletion)' if execute else 'DRY RUN (Analysis only, no changes)'}")
    print("=" * 60)

    async with AsyncSessionLocal() as session:
        # 1. Query all accounts
        acc_stmt = select(AdsAccount).order_by(AdsAccount.user_id, AdsAccount.created_at.asc())
        acc_res = await session.execute(acc_stmt)
        all_accounts = acc_res.scalars().all()

        mock_account_ids = []
        duplicate_account_ids = []

        seen_profiles = {}  # (user_id, profile_id) -> first account id

        for acc in all_accounts:
            # Check mock
            if acc.is_mock or not acc.profile_id:
                mock_account_ids.append(acc.id)
                continue

            # Check duplicate profile for same user
            key = (acc.user_id, acc.profile_id)
            if key in seen_profiles:
                duplicate_account_ids.append(acc.id)
            else:
                seen_profiles[key] = acc.id

        target_ids = list(set(mock_account_ids + duplicate_account_ids))

        print(f"\nTotal Ads Accounts in database: {len(all_accounts)}")
        print(f"  - Mock accounts (is_mock=True or profile_id is None): {len(mock_account_ids)}")
        print(f"  - Duplicate accounts (same user_id & profile_id): {len(duplicate_account_ids)}")
        print(f"  - Total unique account IDs targeted for purge: {len(target_ids)}")

        if not target_ids:
            print("\nNo mock or duplicate accounts found. Database is clean.")
            return

        print(f"\nTargeted Account IDs: {sorted(target_ids)}")

        # 2. Count child records attached to targeted accounts
        camp_stmt = select(Campaign.id).where(Campaign.ads_account_id.in_(target_ids))
        camp_res = await session.execute(camp_stmt)
        campaign_ids = [r[0] for r in camp_res.all()]

        # Related counts
        c_count = len(campaign_ids)
        
        ag_count = 0
        kw_count = 0
        md_count = 0
        st_count = 0
        neg_count = 0
        pl_count = 0
        hist_count = 0

        if campaign_ids:
            ag_stmt = select(func.count(AdGroup.id)).where(AdGroup.campaign_id.in_(campaign_ids))
            ag_count = (await session.execute(ag_stmt)).scalar() or 0

            kw_stmt = select(func.count(Keyword.id)).where(Keyword.campaign_id.in_(campaign_ids))
            kw_count = (await session.execute(kw_stmt)).scalar() or 0

            md_stmt = select(func.count(MetricDaily.id)).where(
                MetricDaily.entity_type == "campaign",
                MetricDaily.entity_id.in_(campaign_ids)
            )
            md_count = (await session.execute(md_stmt)).scalar() or 0

            st_stmt = select(func.count(SearchTerm.id)).where(SearchTerm.campaign_id.in_(campaign_ids))
            st_count = (await session.execute(st_stmt)).scalar() or 0

            neg_stmt = select(func.count(NegativeKeyword.id)).where(NegativeKeyword.campaign_id.in_(campaign_ids))
            neg_count = (await session.execute(neg_stmt)).scalar() or 0

            pl_stmt = select(func.count(CampaignPlacement.id)).where(CampaignPlacement.campaign_id.in_(campaign_ids))
            pl_count = (await session.execute(pl_stmt)).scalar() or 0

            hist_stmt = select(func.count(CampaignHistory.id)).where(CampaignHistory.campaign_id.in_(campaign_ids))
            hist_count = (await session.execute(hist_stmt)).scalar() or 0

        # Drafts and recommendations
        draft_stmt = select(func.count(CampaignDraft.id)).where(CampaignDraft.ads_account_id.in_(target_ids))
        draft_count = (await session.execute(draft_stmt)).scalar() or 0

        rec_stmt = select(func.count(Recommendation.id)).where(Recommendation.ads_account_id.in_(target_ids))
        rec_count = (await session.execute(rec_stmt)).scalar() or 0

        print("\nChild records that would be removed:")
        print(f"  - Campaigns: {c_count}")
        print(f"  - Ad Groups: {ag_count}")
        print(f"  - Keywords: {kw_count}")
        print(f"  - Daily Metrics: {md_count}")
        print(f"  - Search Terms: {st_count}")
        print(f"  - Negative Keywords: {neg_count}")
        print(f"  - Placements: {pl_count}")
        print(f"  - Campaign History: {hist_count}")
        print(f"  - Campaign Drafts: {draft_count}")
        print(f"  - Recommendations: {rec_count}")

        if not execute:
            print("\n" + "=" * 60)
            print("DRY RUN FINISHED: No rows were deleted.")
            print("To delete these rows, execute:")
            print("  python -m scripts.purge_mock_data --execute")
            print("=" * 60)
            return

        # 3. Perform Deletion
        print("\nProceeding with deletion...")

        # Delete recommendations
        rec_ids_stmt = select(Recommendation.id).where(Recommendation.ads_account_id.in_(target_ids))
        rec_ids = [r[0] for r in (await session.execute(rec_ids_stmt)).all()]
        if rec_ids:
            await session.execute(delete(RecommendationEvent).where(RecommendationEvent.recommendation_id.in_(rec_ids)))
            await session.execute(delete(Recommendation).where(Recommendation.id.in_(rec_ids)))

        # Delete drafts
        await session.execute(delete(CampaignDraft).where(CampaignDraft.ads_account_id.in_(target_ids)))

        # Delete campaign children
        if campaign_ids:
            await session.execute(delete(AdGroup).where(AdGroup.campaign_id.in_(campaign_ids)))
            await session.execute(delete(Keyword).where(Keyword.campaign_id.in_(campaign_ids)))
            await session.execute(delete(MetricDaily).where(
                MetricDaily.entity_type == "campaign",
                MetricDaily.entity_id.in_(campaign_ids)
            ))
            await session.execute(delete(SearchTerm).where(SearchTerm.campaign_id.in_(campaign_ids)))
            await session.execute(delete(NegativeKeyword).where(NegativeKeyword.campaign_id.in_(campaign_ids)))
            await session.execute(delete(CampaignPlacement).where(CampaignPlacement.campaign_id.in_(campaign_ids)))
            await session.execute(delete(CampaignHistory).where(CampaignHistory.campaign_id.in_(campaign_ids)))
            await session.execute(delete(Campaign).where(Campaign.id.in_(campaign_ids)))

        # Delete accounts
        await session.execute(delete(AdsAccount).where(AdsAccount.id.in_(target_ids)))

        await session.commit()
        print("PURGE COMPLETE: All targeted rows successfully deleted.")


def main():
    parser = argparse.ArgumentParser(description="Purge mock & duplicate Ads Accounts")
    parser.add_argument("--execute", action="store_true", help="Actually delete rows (default is dry-run)")
    args = parser.parse_args()

    asyncio.run(inspect_and_purge(execute=args.execute))


if __name__ == "__main__":
    main()
