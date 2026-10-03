import random
from datetime import date, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from app.providers.ads.base import AdsProvider
from app.models.campaign import Campaign
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.search_term import SearchTerm
from app.models.metric_daily import MetricDaily
from sqlalchemy import select

class MockAdsProvider(AdsProvider):
    def __init__(self, db: AsyncSession):
        self.db = db

    async def generate_for_new_connection(self, ads_account_id: int):
        # Create campaigns
        campaigns = []
        for i in range(10):
            c = Campaign(
                ads_account_id=ads_account_id,
                name=f"Mock Campaign {i}",
                external_id=f"mock_c_{ads_account_id}_{i}"
            )
            self.db.add(c)
            campaigns.append(c)
        await self.db.commit()
        
        # Create ad groups and keywords
        for c in campaigns:
            for j in range(2):
                ag = AdGroup(campaign_id=c.id, name=f"AdGroup {j}", external_id=f"mock_ag_{c.id}_{j}")
                self.db.add(ag)
                await self.db.flush()
                for k in range(5):
                    kw = Keyword(ad_group_id=ag.id, keyword_text=f"keyword {j} {k}", external_id=f"mock_kw_{ag.id}_{k}", match_type="EXACT")
                    self.db.add(kw)
        await self.db.commit()

        # Generate metrics
        await self._generate_metrics(ads_account_id)

    async def sync_account_data(self, ads_account_id: int):
        pass

    async def _generate_metrics(self, ads_account_id: int):
        # generate daily metrics for last 90 days for account campaigns
        result = await self.db.execute(select(Campaign).where(Campaign.ads_account_id == ads_account_id))
        campaigns = result.scalars().all()
        for i in range(90):
            d = date.today() - timedelta(days=i)
            for c in campaigns:
                m = MetricDaily(
                    date=d,
                    entity_type="campaign",
                    entity_id=c.id,
                    impressions=random.randint(100, 1000),
                    clicks=random.randint(10, 100),
                    spend=random.uniform(5.0, 50.0),
                    sales=random.uniform(10.0, 200.0),
                    organic_sales=random.uniform(20.0, 500.0)
                )
                self.db.add(m)
        await self.db.commit()
