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
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.search_term import SearchTerm
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

def largest_remainder_split_int(total: int, weights: list[float]) -> list[int]:
    if total <= 0:
        return [0] * len(weights)
    w_sum = sum(weights) or 1.0
    norm_w = [w / w_sum for w in weights]
    exact = [total * w for w in norm_w]
    floors = [int(x) for x in exact]
    rem = total - sum(floors)
    indexed_remainders = sorted(
        range(len(weights)),
        key=lambda i: (exact[i] - floors[i]),
        reverse=True,
    )
    res = list(floors)
    for i in range(rem):
        res[indexed_remainders[i]] += 1
    return res

def split_money_last_remainder(total: float, weights: list[float]) -> list[float]:
    if total <= 0:
        return [0.0] * len(weights)
    w_sum = sum(weights) or 1.0
    norm_w = [w / w_sum for w in weights]
    parts = []
    running = 0.0
    for w in norm_w[:-1]:
        val = round(total * w, 2)
        parts.append(val)
        running += val
    last_val = round(total - running, 2)
    parts.append(last_val)
    return parts

def split_campaign_metrics_to_placements(
    impressions: int,
    clicks: int,
    spend: float,
    sales: float,
    orders: int,
    noise_seed: int | None = None,
) -> dict[str, dict[str, Any]]:
    # Split across TOP_OF_SEARCH (45%), REST_OF_SEARCH (35%), PRODUCT_PAGES (20%)
    weights = [0.45, 0.35, 0.20]

    split_imps = largest_remainder_split_int(impressions, weights)
    split_clks = largest_remainder_split_int(clicks, weights)
    split_spend = split_money_last_remainder(spend, weights)
    split_orders = largest_remainder_split_int(orders, weights)
    split_sales = split_money_last_remainder(sales, weights)

    placements = ["TOP_OF_SEARCH", "REST_OF_SEARCH", "PRODUCT_PAGES"]
    res = {}
    for idx, p in enumerate(placements):
        res[p] = {
            "placement": p,
            "impressions": split_imps[idx],
            "clicks": split_clks[idx],
            "spend": split_spend[idx],
            "sales": split_sales[idx],
            "orders": split_orders[idx],
        }
    return res

class MockAdsProvider(AdsProvider):
    def __init__(self, db: AsyncSession):
        self.db = db

    def generate_asin(self):
        return "B0" + "".join(random.choices(string.ascii_uppercase + string.digits, k=8))

    def generate_sku(self):
        return "SKU-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

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
                sku=self.generate_sku(),
                title=f"Mock Product {i} Premium",
                image_url=f"https://via.placeholder.com/150?text=Product+{i}",
                category=categories[i % len(categories)],
                price=round(random.uniform(15.0, 150.0), 2)
            )
            self.db.add(p)
            products.append(p)
        await self.db.commit()

        campaign_configs = [
            {
                "name": "Mock Winning Bestseller Campaign 0 (MANUAL)",
                "campaign_type": "SPONSORED_PRODUCTS",
                "targeting_type": "MANUAL",
                "portfolio_name": "Bestsellers",
                "daily_budget": 2500.0,
                "bidding_strategy": "UP_AND_DOWN",
                "top_pct": 50,
                "product_pct": 25,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "winning",
            },
            {
                "name": "Mock Winning Festive Auto Campaign 1 (AUTO)",
                "campaign_type": "SPONSORED_PRODUCTS",
                "targeting_type": "AUTO",
                "portfolio_name": "Festive Sale",
                "daily_budget": 3000.0,
                "bidding_strategy": "UP_AND_DOWN",
                "top_pct": 40,
                "product_pct": 20,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "winning",
            },
            {
                "name": "Mock Bleeding New Launches Campaign 2 (MANUAL)",
                "campaign_type": "SPONSORED_PRODUCTS",
                "targeting_type": "MANUAL",
                "portfolio_name": "New Launches",
                "daily_budget": 1000.0,
                "bidding_strategy": "DOWN_ONLY",
                "top_pct": 0,
                "product_pct": 0,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "bleeding",
            },
            {
                "name": "Mock Bleeding Festive Brands Campaign 3 (MANUAL)",
                "campaign_type": "SPONSORED_BRANDS",
                "targeting_type": "MANUAL",
                "portfolio_name": "Festive Sale",
                "daily_budget": 800.0,
                "bidding_strategy": "DOWN_ONLY",
                "top_pct": 0,
                "product_pct": 0,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "bleeding",
            },
            {
                "name": "Mock Out of Budget Campaign 4 (MANUAL)",
                "campaign_type": "SPONSORED_PRODUCTS",
                "targeting_type": "MANUAL",
                "portfolio_name": "Bestsellers",
                "daily_budget": 500.0,
                "bidding_strategy": "DOWN_ONLY",
                "top_pct": 20,
                "product_pct": 10,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "OUT_OF_BUDGET",
                "profile": "out_of_budget",
            },
            {
                "name": "Mock Paused Display Campaign 5 (MANUAL)",
                "campaign_type": "SPONSORED_DISPLAY",
                "targeting_type": "MANUAL",
                "portfolio_name": "New Launches",
                "daily_budget": 1200.0,
                "bidding_strategy": "FIXED",
                "top_pct": 0,
                "product_pct": 0,
                "rest_pct": 0,
                "state": "PAUSED",
                "delivery_status": "PAUSED",
                "profile": "paused",
            },
            {
                "name": "Mock Archived Campaign 6 (MANUAL)",
                "campaign_type": "SPONSORED_PRODUCTS",
                "targeting_type": "MANUAL",
                "portfolio_name": "Festive Sale",
                "daily_budget": 1500.0,
                "bidding_strategy": "DOWN_ONLY",
                "top_pct": 0,
                "product_pct": 0,
                "rest_pct": 0,
                "state": "ARCHIVED",
                "delivery_status": "ARCHIVED",
                "profile": "ended",
            },
            {
                "name": "Mock Leaking Campaign 7 (MANUAL)",
                "campaign_type": "SPONSORED_PRODUCTS",
                "targeting_type": "MANUAL",
                "portfolio_name": "Bestsellers",
                "daily_budget": 1500.0,
                "bidding_strategy": "DOWN_ONLY",
                "top_pct": 10,
                "product_pct": 0,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "leaking",
            },
            {
                "name": "Mock High Acos Campaign 8 (MANUAL)",
                "campaign_type": "SPONSORED_BRANDS",
                "targeting_type": "MANUAL",
                "portfolio_name": "Festive Sale",
                "daily_budget": 2000.0,
                "bidding_strategy": "UP_AND_DOWN",
                "top_pct": 30,
                "product_pct": 10,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "high_acos",
            },
            {
                "name": "Mock Efficient Display Campaign 9 (MANUAL)",
                "campaign_type": "SPONSORED_DISPLAY",
                "targeting_type": "MANUAL",
                "portfolio_name": "New Launches",
                "daily_budget": 1800.0,
                "bidding_strategy": "FIXED",
                "top_pct": 15,
                "product_pct": 15,
                "rest_pct": 0,
                "state": "ENABLED",
                "delivery_status": "DELIVERING",
                "profile": "efficient",
            },
        ]

        campaigns = []
        for i, cfg in enumerate(campaign_configs):
            profile = cfg["profile"]
            state = cfg["state"]
            start_date = today - timedelta(days=random.randint(45, 300))
            end_date = None
            if profile == "ended":
                start_date = today - timedelta(days=60)
                end_date = today - timedelta(days=10)
            
            c = Campaign(
                ads_account_id=ads_account_id,
                amazon_campaign_id=f"amzn_c_{ads_account_id}_{i}",
                external_id=f"mock_c_{ads_account_id}_{i}",
                name=cfg["name"],
                campaign_type=cfg["campaign_type"],
                targeting_type=cfg["targeting_type"],
                portfolio_name=cfg["portfolio_name"],
                state=state,
                delivery_status=cfg["delivery_status"],
                bidding_strategy=cfg["bidding_strategy"],
                placement_top_of_search_pct=cfg["top_pct"],
                placement_product_pages_pct=cfg["product_pct"],
                placement_rest_of_search_pct=cfg["rest_pct"],
                placement_top_pct=cfg["top_pct"],
                placement_product_page_pct=cfg["product_pct"],
                placement_rest_pct=cfg["rest_pct"],
                start_date=start_date,
                end_date=end_date,
                daily_budget=cfg["daily_budget"],
                negative_brands=["UnwantedBrandA", "CompetitorBrandB"],
            )
            self.db.add(c)
            campaigns.append(c)
        await self.db.commit()

        for c in campaigns:
            p = random.choice(products)
            c.product_id = p.id
            cp = CampaignProduct(campaign_id=c.id, product_id=p.id)
            self.db.add(cp)
            
            ag = AdGroup(campaign_id=c.id, name="Main Ad Group", default_bid=15.0)
            self.db.add(ag)
            await self.db.flush()
            
            c_name_lower = c.name.lower()
            if c.targeting_type == "MANUAL":
                match_types = ["BROAD", "PHRASE", "EXACT"]
                for k in range(12):
                    if k == 0 and "bleeding" in c_name_lower:
                        kw_text = f"bleeding target {c.id}"
                    elif k == 0 and ("high_acos" in c_name_lower or "high acos" in c_name_lower):
                        kw_text = f"high acos target {c.id}"
                    elif k == 0 and "efficient" in c_name_lower:
                        kw_text = f"efficient target {c.id}"
                    else:
                        kw_text = f"keyword {k} {c.id}"
                    m_type = match_types[k % len(match_types)]
                    kw = Keyword(
                        ad_group_id=ag.id,
                        keyword_text=kw_text,
                        match_type=m_type,
                        bid=round(random.uniform(8.0, 35.0), 2),
                        state="ENABLED"
                    )
                    self.db.add(kw)

                # Product ASIN Targets
                for _ in range(2):
                    t_asin = self.generate_asin()
                    tg = Target(
                        campaign_id=c.id,
                        ad_group_id=ag.id,
                        target_type="PRODUCT",
                        expression_type="ASIN_SAME_AS",
                        expression_value=t_asin,
                        kind="ASIN",
                        value=t_asin,
                        bid=round(random.uniform(10.0, 25.0), 2),
                        state="ENABLED"
                    )
                    self.db.add(tg)

                nk1 = NegativeKeyword(campaign_id=c.id, keyword_text="cheap free discount", match_type="NEGATIVE_EXACT")
                nk2 = NegativeKeyword(campaign_id=c.id, keyword_text="repair manual", match_type="NEGATIVE_PHRASE")
                self.db.add(nk1)
                self.db.add(nk2)
            else:
                for val, b_val in [("CLOSE_MATCH", 22.0), ("LOOSE_MATCH", 14.0), ("SUBSTITUTES", 18.0), ("COMPLEMENTS", 12.0)]:
                    tg = Target(
                        campaign_id=c.id,
                        ad_group_id=ag.id,
                        target_type="AUTO_GROUP",
                        expression_type=val,
                        expression_value=val,
                        kind="AUTO_GROUP",
                        value=val,
                        bid=b_val,
                        state="ENABLED"
                    )
                    self.db.add(tg)
                
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
        placement_rows = []
        for c in campaigns:
            c_name_lower = c.name.lower()
            profile = "average"
            if "winning" in c_name_lower: profile = "winning"
            elif "leaking" in c_name_lower: profile = "leaking"
            elif "bleeding" in c_name_lower: profile = "bleeding"
            elif "out of budget" in c_name_lower: profile = "out_of_budget"
            elif "high_acos" in c_name_lower or "high acos" in c_name_lower: profile = "high_acos"
            elif "efficient" in c_name_lower: profile = "efficient"
            elif "new" in c_name_lower: profile = "new"
            
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

                if profile == "winning" and i < 30:
                    orders = 3
                    sales = round(orders * p_price * 1.0, 2)
                    spend = round(sales / 5.5, 2)  # ROAS = 5.5 >= 4, ACOS ~ 18% <= 20%
                    clicks = max(orders, 10)
                    impressions = clicks * 40
                    organic_sales = round(sales * 1.2, 2)
                elif profile == "leaking" and i < 7:
                    spend = 65.0 if account.marketplace == "IN" else 15.0
                    orders = 1 if i == 0 else 0
                    sales = 20.0 if orders == 1 else 0.0
                    clicks = 12
                    impressions = 250
                    organic_sales = 0.0
                elif profile == "bleeding":
                    impressions = 150
                    clicks = 4
                    spend = 20.0 if i < 30 else 5.0
                    orders = 0
                    sales = 0.0
                    organic_sales = 0.0
                elif profile == "out_of_budget":
                    if i == 0:
                        spend = round(c.daily_budget * 0.98, 2)
                        clicks = 25
                        impressions = 500
                        orders = 2
                        sales = round(orders * p_price, 2)
                        organic_sales = round(sales * 1.1, 2)
                    else:
                        spend = round(random.uniform(20.0, 50.0), 2)
                        clicks = 6
                        impressions = 120
                        orders = 1
                        sales = round(p_price, 2)
                        organic_sales = 0.0
                else:
                    base_imp = random.randint(100, 2000)
                    noise = random.uniform(0.8, 1.2)
                    impressions = int(base_imp * noise)
                    ctr = random.uniform(0.002, 0.012)
                    clicks = round(impressions * ctr)
                    cpc = random.uniform(rules["min_bid"], min(rules["max_bid"], 3.0))
                    spend = round(clicks * cpc, 2)
                    cvr = random.uniform(0.05, 0.15)
                    orders = sum(1 for _ in range(clicks) if random.random() < cvr)
                    sales = round(orders * p_price * random.uniform(0.95, 1.05), 2)
                    organic_sales = round(sales * random.uniform(0.8, 2.5), 2)
                
                rows.append({
                    "date": d, "entity_type": "campaign", "entity_id": c.id,
                    "impressions": impressions, "clicks": clicks, "spend": spend,
                    "sales": sales, "orders": orders, "organic_sales": organic_sales
                })

                p_splits = split_campaign_metrics_to_placements(
                    impressions=impressions,
                    clicks=clicks,
                    spend=spend,
                    sales=sales,
                    orders=orders,
                    noise_seed=c.id * 1000 + d.toordinal(),
                )
                for p_data in p_splits.values():
                    placement_rows.append({
                        "campaign_id": c.id,
                        "placement": p_data["placement"],
                        "date": d,
                        "impressions": p_data["impressions"],
                        "clicks": p_data["clicks"],
                        "spend": p_data["spend"],
                        "sales": p_data["sales"],
                        "orders": p_data["orders"],
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

                    special_kw = next((kw for kw in keywords if kw.keyword_text.startswith(("bleeding target", "high acos target", "efficient target"))), None)
                    if special_kw and special_kw.keyword_text.startswith("bleeding target"):
                        for kw in keywords:
                            if kw.id == special_kw.id:
                                kw_c = 1 if i < 30 else 0
                                kw_imp = 20 if i < 30 else 0
                                kw_sp = round(kw_c * rules["min_bid"] * 2.0, 2)
                                rows.append({
                                    "date": d, "entity_type": "keyword", "entity_id": kw.id,
                                    "impressions": kw_imp, "clicks": kw_c,
                                    "spend": kw_sp, "sales": 0.0,
                                    "orders": 0, "organic_sales": 0.0
                                })
                            else:
                                rows.append({
                                    "date": d, "entity_type": "keyword", "entity_id": kw.id,
                                    "impressions": 10, "clicks": 0,
                                    "spend": 0.0, "sales": 0.0,
                                    "orders": 0, "organic_sales": 0.0
                                })
                    elif special_kw and special_kw.keyword_text.startswith("high acos target"):
                        for kw in keywords:
                            if kw.id == special_kw.id:
                                kw_c = 2 if i < 7 else 0
                                kw_imp = 40 if i < 7 else 0
                                kw_sp = 15.0 if i < 7 else 0.0
                                kw_ord = 1 if (i == 0) else 0
                                kw_sal = 15.0 if (i == 0) else 0.0
                                rows.append({
                                    "date": d, "entity_type": "keyword", "entity_id": kw.id,
                                    "impressions": kw_imp, "clicks": kw_c,
                                    "spend": kw_sp, "sales": kw_sal,
                                    "orders": kw_ord, "organic_sales": 0.0
                                })
                            else:
                                rows.append({
                                    "date": d, "entity_type": "keyword", "entity_id": kw.id,
                                    "impressions": 10, "clicks": 0,
                                    "spend": 0.0, "sales": 0.0,
                                    "orders": 0, "organic_sales": 0.0
                                })
                    elif special_kw and special_kw.keyword_text.startswith("efficient target"):
                        for kw in keywords:
                            if kw.id == special_kw.id:
                                is_active_day = (i < 30 and i % 3 == 0)
                                kw_c = 2 if is_active_day else 0
                                kw_imp = 35 if is_active_day else 0
                                kw_sp = 2.0 if is_active_day else 0.0
                                kw_ord = 1 if i in (0, 6, 12) else 0
                                kw_sal = 50.0 if i in (0, 6, 12) else 0.0
                                rows.append({
                                    "date": d, "entity_type": "keyword", "entity_id": kw.id,
                                    "impressions": kw_imp, "clicks": kw_c,
                                    "spend": kw_sp, "sales": kw_sal,
                                    "orders": kw_ord, "organic_sales": 0.0
                                })
                            else:
                                rows.append({
                                    "date": d, "entity_type": "keyword", "entity_id": kw.id,
                                    "impressions": 10, "clicks": 0,
                                    "spend": 0.0, "sales": 0.0,
                                    "orders": 0, "organic_sales": 0.0
                                })
                    else:
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

        if placement_rows:
            chunk_size = 5000
            for i in range(0, len(placement_rows), chunk_size):
                await self.db.execute(insert(PlacementMetricDaily), placement_rows[i:i+chunk_size])
            await self.db.commit()

        # Ensure search terms exist with coherent orders & believable distribution
        st_res = await self.db.execute(select(SearchTerm.id).where(
            SearchTerm.campaign_id.in_([c.id for c in campaigns])
        ).limit(1))
        if not st_res.scalars().first():
            st_rows = []
            for c in campaigns:
                if c.state != "ENABLED":
                    continue
                ag_res = await self.db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id).limit(1))
                ag = ag_res.scalars().first()
                if not ag:
                    continue
                kw_res = await self.db.execute(select(Keyword).where(Keyword.ad_group_id == ag.id).limit(1))
                kw = kw_res.scalars().first()
                if not kw:
                    continue

                p_price = c.product.price if c.product else 50.0
                min_spend = 25.0 if account.marketplace == "IN" else 15.0

                wasted_terms = [
                    "cheap discount coupon promo",
                    "free download manual pdf",
                    "broken parts replacement used",
                    "refurbished second hand bargain",
                    "how to repair tutorial guide",
                ]
                for idx, term in enumerate(wasted_terms):
                    st_rows.append(SearchTerm(
                        date=today - timedelta(days=2 + idx * 3),
                        search_term=term,
                        campaign_id=c.id,
                        ad_group_id=ag.id,
                        keyword_id=kw.id,
                        clicks=10 + idx,
                        impressions=(10 + idx) * 20,
                        spend=round(min_spend + (idx * 5.0), 2),
                        sales=0.0,
                        orders=0,
                    ))

                converting_terms = [
                    "best organic premium quality",
                    "top rated durable authentic",
                    "official brand pro edition",
                    "ultra comfort daily wear",
                ]
                for idx, term in enumerate(converting_terms):
                    ord_cnt = 2 + idx
                    s_val = round(ord_cnt * p_price * 1.0, 2)
                    sp_val = round(s_val * 0.18, 2)  # ACOS = 18% <= 30%
                    st_rows.append(SearchTerm(
                        date=today - timedelta(days=1 + idx * 4),
                        search_term=term,
                        campaign_id=c.id,
                        ad_group_id=ag.id,
                        keyword_id=kw.id,
                        clicks=max(ord_cnt, 5 + idx),
                        impressions=(5 + idx) * 25,
                        spend=sp_val,
                        sales=s_val,
                        orders=ord_cnt,
                    ))

                regular_terms = [
                    "travel pack portable lightweight",
                    "black color size standard",
                ]
                for idx, term in enumerate(regular_terms):
                    st_rows.append(SearchTerm(
                        date=today - timedelta(days=5 + idx * 5),
                        search_term=term,
                        campaign_id=c.id,
                        ad_group_id=ag.id,
                        keyword_id=kw.id,
                        clicks=4,
                        impressions=80,
                        spend=8.0,
                        sales=round(p_price, 2),
                        orders=1,
                    ))
                break

            if st_rows:
                self.db.add_all(st_rows)
                await self.db.commit()

    def search_brands(self, q: str = "") -> list[str]:
        fictional_brands = [
            "ApexStyle", "AuraCraft", "BoldWave", "BreezeLiving", "BrightNova",
            "CoreFusion", "CozyNest", "CrestPeak", "EchoPulse", "Elementix",
            "EverGlow", "FlowState", "FrontierGoods", "FusionWear", "GlideGear",
            "HavenPure", "HorizonPro", "IgniteWorks", "InfinityCraft", "IronClad",
            "LuminaLife", "LunaBloom", "ModernHaven", "MotionTech", "NexusPrime",
            "NovaForge", "OasisHome", "OmniFit", "PeakForm", "PolarisSupply",
            "PrismWorks", "PulseActive", "QuantumLiving", "RadiantRoots", "SolaceGear",
            "SummitLine", "TerraVibe", "TrueNorth", "UrbanCraft", "ZenithSupply"
        ]
        q_clean = q.strip().lower()
        if not q_clean:
            return fictional_brands
        return [b for b in fictional_brands if q_clean in b.lower()]

    @staticmethod
    def suggest_bid(text: str, default_bid: float, rules: dict) -> dict:
        import hashlib
        h = int(hashlib.md5(text.encode()).hexdigest()[:8], 16) % 100 / 100
        median = min(max(default_bid * (0.6 + 0.9 * h), rules["min_bid"]), rules["max_bid"])
        return {
            "low": round(max(rules["min_bid"], median * 0.7), 2),
            "median": round(median, 2),
            "high": round(min(rules["max_bid"], median * 1.4), 2)
        }


async def backfill_placement_metrics(db: AsyncSession, ads_account_id: int | None = None) -> int:
    """Backfills placement_metrics_daily from existing campaign MetricDaily rows."""
    camp_query = select(Campaign.id)
    if ads_account_id is not None:
        camp_query = camp_query.where(Campaign.ads_account_id == ads_account_id)
    camp_ids = (await db.execute(camp_query)).scalars().all()
    if not camp_ids:
        return 0

    existing_p = set(
        (row[0], row[1])
        for row in (
            await db.execute(
                select(PlacementMetricDaily.campaign_id, PlacementMetricDaily.date)
                .where(PlacementMetricDaily.campaign_id.in_(camp_ids))
            )
        ).all()
    )

    m_rows = (
        await db.execute(
            select(MetricDaily)
            .where(
                MetricDaily.entity_type == "campaign",
                MetricDaily.entity_id.in_(camp_ids),
            )
        )
    ).scalars().all()

    new_p_rows = []
    for m in m_rows:
        if (m.entity_id, m.date) in existing_p:
            continue
        p_splits = split_campaign_metrics_to_placements(
            impressions=m.impressions or 0,
            clicks=m.clicks or 0,
            spend=float(m.spend or 0.0),
            sales=float(m.sales or 0.0),
            orders=m.orders or 0,
            noise_seed=m.entity_id * 1000 + m.date.toordinal(),
        )
        for p_data in p_splits.values():
            new_p_rows.append({
                "campaign_id": m.entity_id,
                "placement": p_data["placement"],
                "date": m.date,
                "impressions": p_data["impressions"],
                "clicks": p_data["clicks"],
                "spend": p_data["spend"],
                "sales": p_data["sales"],
                "orders": p_data["orders"],
            })

    if new_p_rows:
        chunk_size = 5000
        for i in range(0, len(new_p_rows), chunk_size):
            await db.execute(insert(PlacementMetricDaily), new_p_rows[i:i+chunk_size])
        await db.commit()

    return len(new_p_rows)


async def backfill_mock_search_terms_and_metrics(db: AsyncSession, ads_account_id: int):
    """One-off helper to backfill search terms, placements, and metrics for already-connected mock accounts."""
    provider = MockAdsProvider(db)
    await provider.sync_account_data(ads_account_id)
    await backfill_placement_metrics(db, ads_account_id)
