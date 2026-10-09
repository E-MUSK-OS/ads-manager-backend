import random
from datetime import timedelta
import string
import math
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, insert
from sqlalchemy.orm import selectinload

from app.providers.ads.base import AdsProvider
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_product import CampaignProduct
from app.models.negative_keyword import NegativeKeyword
from app.models.target import Target
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.metric_daily import MetricDaily
from app.models.product import Product
from app.models.product_review import ProductReview
from app.core.marketplace_rules import MARKETPLACE_RULES, today_for

def split_int(value: int, parts: int):
    if parts <= 0: return []
    base = value // parts
    rem = value % parts
    return [base + 1 if i < rem else base for i in range(parts)]

def split_float(value: float, parts: int):
    if parts <= 0: return []
    base = round(value / parts, 2)
    res = [base] * parts
    res[-1] = round(value - sum(res[:-1]), 2)
    return res

class MockAdsProvider(AdsProvider):
    def __init__(self, db: AsyncSession):
        self.db = db

    def generate_asin(self):
        return "B0" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

    async def generate_for_new_connection(self, ads_account_id: int):
        account = (await self.db.execute(select(AdsAccount).where(AdsAccount.id == ads_account_id))).scalars().first()
        rules = MARKETPLACE_RULES[account.marketplace]
        today = today_for(account.marketplace)

        products = []
        categories = ["Electronics", "Home", "Kitchen", "Toys", "Beauty"]
        for i in range(8):
            p = Product(
                ads_account_id=ads_account_id,
                asin=self.generate_asin(),
                title=f"Mock Product {i} Premium",
                image_url=f"https://via.placeholder.com/150?text=Product+{i}",
                category=categories[i % len(categories)],
                price=round(random.uniform(15.0, 150.0), 2)
            )
            self.db.add(p)
            products.append(p)
        await self.db.commit()

        campaigns = []
        profiles = ["good", "average", "bleeding", "new", "scheduled", "ended"]
        for i in range(12):
            profile = profiles[i % len(profiles)]
            state = "ENABLED"
            start_date = today - timedelta(days=random.randint(30, 400))
            end_date = None
            if profile == "new": start_date = today - timedelta(days=3)
            elif profile == "scheduled": start_date = today + timedelta(days=1)
            elif profile == "ended":
                start_date = today - timedelta(days=60)
                end_date = today - timedelta(days=10)
                state = "PAUSED"
            elif profile == "paused": state = "PAUSED"
            
            targeting = "AUTO" if i % 2 == 0 else "MANUAL"
            c = Campaign(
                ads_account_id=ads_account_id,
                name=f"Mock {profile.capitalize()} Campaign {i} ({targeting})",
                external_id=f"mock_c_{ads_account_id}_{i}",
                state=state,
                targeting_type=targeting,
                bidding_strategy="AUTO_FOR_SALES",
                start_date=start_date,
                end_date=end_date,
                daily_budget=round(random.uniform(rules["min_budget"] * 2, rules["max_bid"] * 20), 2),
            )
            self.db.add(c)
            campaigns.append(c)
        await self.db.commit()

        for c in campaigns:
            p = random.choice(products)
            c.product_id = p.id
            cp = CampaignProduct(campaign_id=c.id, product_id=p.id)
            self.db.add(cp)
            
            ag = AdGroup(campaign_id=c.id, name="Main Ad Group", default_bid=round(rules["min_bid"] * 5, 2))
            self.db.add(ag)
            await self.db.flush()
            
            if c.targeting_type == "MANUAL":
                for k in range(random.randint(5, 35)):
                    kw = Keyword(ad_group_id=ag.id, keyword_text=f"keyword {k} {c.id}", match_type="BROAD", state="ENABLED")
                    self.db.add(kw)
            else:
                for k in ["CLOSE_MATCH", "LOOSE_MATCH", "SUBSTITUTES", "COMPLEMENTS"]:
                    tg = Target(ad_group_id=ag.id, kind="AUTO_GROUP", value=k, state="ENABLED")
                    self.db.add(tg)
                    
            if random.random() > 0.5:
                nk = NegativeKeyword(campaign_id=c.id, keyword_text="cheap", match_type="NEGATIVE_EXACT")
                self.db.add(nk)
                
        await self.db.commit()
        await self.sync_account_data(ads_account_id)

    async def sync_account_data(self, ads_account_id: int):
        account = (await self.db.execute(select(AdsAccount).where(AdsAccount.id == ads_account_id))).scalars().first()
        rules = MARKETPLACE_RULES[account.marketplace]
        today = today_for(account.marketplace)
        
        # Load campaigns with ad_groups and their keywords/targets
        c_result = await self.db.execute(select(Campaign).where(Campaign.ads_account_id == ads_account_id).options(
            selectinload(Campaign.product)
        ))
        campaigns = c_result.scalars().all()
        
        m_result = await self.db.execute(select(MetricDaily.entity_id, MetricDaily.date).where(
            MetricDaily.entity_type == "campaign", 
            MetricDaily.entity_id.in_([c.id for c in campaigns])
        ))
        existing_metrics = set((row[0], row[1]) for row in m_result.all())
        
        rows = []
        for c in campaigns:
            profile = "average"
            if "new" in c.name.lower(): profile = "new"
            elif "bleeding" in c.name.lower(): profile = "bleeding"
            
            p_price = c.product.price if c.product else 50.0

            ag_res = await self.db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id))
            ad_groups = ag_res.scalars().all()

            for i in range(90):
                d = today - timedelta(days=i)
                if (c.id, d) in existing_metrics: continue
                if d < c.start_date: continue
                if c.end_date and d > c.end_date: continue
                if c.state in ("PAUSED", "ARCHIVED") and d > c.updated_at.date():
                     if random.random() > 0.1: continue

                base_imp = random.randint(100, 2000)
                noise = random.uniform(0.8, 1.2)
                impressions = int(base_imp * noise)
                ctr = random.uniform(0.002, 0.012)
                clicks = round(impressions * ctr)
                cpc = random.uniform(rules["min_bid"], min(rules["max_bid"], 3.0))
                spend = round(clicks * cpc, 2)
                
                cvr = 0.0 if profile == "bleeding" else random.uniform(0.05, 0.15)
                orders = sum(1 for _ in range(clicks) if random.random() < cvr)
                sales = round(orders * p_price * random.uniform(0.95, 1.05), 2)
                organic_sales = round(sales * random.uniform(0.8, 2.5), 2)
                
                rows.append({
                    "date": d, "entity_type": "campaign", "entity_id": c.id,
                    "impressions": impressions, "clicks": clicks, "spend": spend,
                    "sales": sales, "orders": orders, "organic_sales": organic_sales
                })
                
                # split to ad groups
                if not ad_groups: continue
                ag_imps = split_int(impressions, len(ad_groups))
                ag_clks = split_int(clicks, len(ad_groups))
                ag_spends = split_float(spend, len(ad_groups))
                ag_orders = split_int(orders, len(ad_groups))
                ag_sales = split_float(sales, len(ad_groups))

                for ag_idx, ag in enumerate(ad_groups):
                    rows.append({
                        "date": d, "entity_type": "ad_group", "entity_id": ag.id,
                        "impressions": ag_imps[ag_idx], "clicks": ag_clks[ag_idx],
                        "spend": ag_spends[ag_idx], "sales": ag_sales[ag_idx],
                        "orders": ag_orders[ag_idx], "organic_sales": 0.0
                    })

                    # split to keywords/targets
                    kw_res = await self.db.execute(select(Keyword).where(Keyword.ad_group_id == ag.id))
                    keywords = kw_res.scalars().all()
                    if not keywords:
                        continue

                    num_kws = len(keywords)
                    kw_imps = split_int(ag_imps[ag_idx], num_kws)
                    kw_clks = split_int(ag_clks[ag_idx], num_kws)
                    kw_spends = split_float(ag_spends[ag_idx], num_kws)
                    kw_orders = split_int(ag_orders[ag_idx], num_kws)
                    kw_sales = split_float(ag_sales[ag_idx], num_kws)

                    for kw_idx, kw in enumerate(keywords):
                        rows.append({
                            "date": d, "entity_type": "keyword", "entity_id": kw.id,
                            "impressions": kw_imps[kw_idx], "clicks": kw_clks[kw_idx],
                            "spend": kw_spends[kw_idx], "sales": kw_sales[kw_idx],
                            "orders": kw_orders[kw_idx], "organic_sales": 0.0
                        })
                
        if rows:
            chunk_size = 5000
            for i in range(0, len(rows), chunk_size):
                await self.db.execute(insert(MetricDaily), rows[i:i+chunk_size])
            await self.db.commit()
