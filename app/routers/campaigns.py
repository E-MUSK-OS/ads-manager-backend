from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from typing import List, Optional
from datetime import date, timedelta
import math

from app.database import get_db
from app.dependencies import get_current_user, assert_owned_account, load_owned_campaign
from app.models.user import User
from app.models.campaign import Campaign
from app.models.campaign_product import CampaignProduct
from app.models.ads_account import AdsAccount
from app.models.metric_daily import MetricDaily
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.search_term import SearchTerm
from app.models.negative_keyword import NegativeKeyword
from app.models.target import Target
from app.models.product import Product
from app.models.user_campaign_settings import UserCampaignSettings
from app.models.campaign_draft import CampaignDraft
from app.models.placement_metric_daily import PlacementMetricDaily
from app.models.campaign_history import CampaignHistory
from app.models.recommendation import Recommendation
from app.schemas.campaign import (
    CampaignResponse, CampaignCreate, CampaignUpdate, StateUpdate, BulkActionRequest,
    BidSuggestionsRequest, ForecastRequest, NegativeKeywordIn,
    UserCampaignSettingsResponse, UserCampaignSettingsUpdate, clean_keyword_text,
    BulkCampaignActionIn, PlacementsUpdate, TargetPatch, SearchTermHarvestIn,
    SearchTermNegateIn, NegativeIn, CampaignWizardIn
)
import uuid
from app.services.campaign_service import create_campaign, validate_create, load_detail
from app.services.metrics_service import (
    get_report, get_summary, determine_health, derive, get_default_health_rules,
    get_or_create_user_campaign_settings, compute_period_deltas
)
from app.core.marketplace_rules import MARKETPLACE_RULES, today_for, marketplace_or_us, marketplace_or_in, compute_max_effective_bid
from app.core.exceptions import ServiceValidationError
from app.providers.ads.mock_provider import MockAdsProvider

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

@router.get("/meta")
async def get_campaigns_meta(
    ads_account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    rules = marketplace_or_in(account.marketplace)
    today = today_for(account.marketplace)

    # Portfolios in account
    port_res = await db.execute(
        select(Campaign.portfolio_name)
        .where(Campaign.ads_account_id == ads_account_id, Campaign.portfolio_name.isnot(None))
        .distinct()
    )
    portfolios = sorted([p for p in port_res.scalars().all() if p])

    # Status counts
    camp_res = await db.execute(
        select(Campaign.delivery_status, Campaign.state)
        .where(Campaign.ads_account_id == ads_account_id)
    )
    camps = camp_res.all()
    all_count = len(camps)
    delivering_count = sum(1 for c in camps if c[0] == "DELIVERING" and c[1] == "ENABLED")
    oob_count = sum(1 for c in camps if c[0] == "OUT_OF_BUDGET" and c[1] == "ENABLED")
    paused_count = sum(1 for c in camps if c[1] == "PAUSED")
    archived_count = sum(1 for c in camps if c[1] == "ARCHIVED")

    # Drafts count
    drafts_count = (await db.execute(
        select(func.count(CampaignDraft.id))
        .where(CampaignDraft.ads_account_id == ads_account_id)
    )).scalar() or 0

    return {
        "currency": {
            "code": rules["currency_code"],
            "symbol": rules["currency_symbol"],
            "locale": rules["locale"]
        },
        "limits": {
            "min_daily_budget": rules["min_daily_budget"],
            "max_daily_budget": rules["max_daily_budget"],
            "min_bid": rules["min_bid"],
            "max_bid": rules["max_bid"],
            "default_bid": rules["default_bid"]
        },
        "today": str(today),
        "portfolios": portfolios,
        "status_counts": {
            "ALL": all_count,
            "DELIVERING": delivering_count,
            "OUT_OF_BUDGET": oob_count,
            "PAUSED": paused_count,
            "ARCHIVED": archived_count,
            "DRAFTS": drafts_count
        }
    }

@router.get("/timeseries")
async def get_campaigns_timeseries(
    ads_account_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    q: Optional[str] = None,
    state: Optional[str] = None,
    delivery_status: Optional[str] = None,
    campaign_type: Optional[str] = None,
    targeting_type: Optional[str] = None,
    bidding_strategy: Optional[str] = None,
    portfolio: Optional[str] = None,
    granularity: str = "DAILY",
    compare_previous: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    today = today_for(account.marketplace)
    end = end_date or today
    start = start_date or (end - timedelta(days=6))

    camp_q = select(Campaign.id).where(Campaign.ads_account_id == ads_account_id)
    if state: camp_q = camp_q.where(Campaign.state == state.upper())
    if delivery_status: camp_q = camp_q.where(Campaign.delivery_status == delivery_status.upper())
    if campaign_type: camp_q = camp_q.where(Campaign.campaign_type == campaign_type.upper())
    if targeting_type: camp_q = camp_q.where(Campaign.targeting_type == targeting_type.upper())
    if bidding_strategy: camp_q = camp_q.where(Campaign.bidding_strategy == bidding_strategy.upper())
    if portfolio:
        if portfolio.lower() == "uncategorized":
            camp_q = camp_q.where(Campaign.portfolio_name.is_(None))
        else:
            camp_q = camp_q.where(Campaign.portfolio_name == portfolio)

    camp_ids = (await db.execute(camp_q)).scalars().all()
    if not camp_ids:
        return {"series": [], "previous_series": []}

    m_res = await db.execute(
        select(
            MetricDaily.date,
            func.coalesce(func.sum(MetricDaily.impressions), 0),
            func.coalesce(func.sum(MetricDaily.clicks), 0),
            func.coalesce(func.sum(MetricDaily.spend), 0.0),
            func.coalesce(func.sum(MetricDaily.sales), 0.0),
            func.coalesce(func.sum(MetricDaily.orders), 0),
        ).where(
            MetricDaily.entity_type == "campaign",
            MetricDaily.entity_id.in_(camp_ids),
            MetricDaily.date >= start,
            MetricDaily.date <= end
        ).group_by(MetricDaily.date).order_by(MetricDaily.date)
    )

    series = []
    date_map = {row[0]: row for row in m_res.all()}
    curr_d = start
    while curr_d <= end:
        row = date_map.get(curr_d)
        if row:
            imp, clk, spd, sls, ord_cnt = int(row[1]), int(row[2]), round(float(row[3]), 2), round(float(row[4]), 2), int(row[5])
        else:
            imp, clk, spd, sls, ord_cnt = 0, 0, 0.0, 0.0, 0
        derived = derive(imp, clk, spd, sls, ord_cnt)
        series.append({"date": str(curr_d), **derived})
        curr_d += timedelta(days=1)

    previous_series = []
    if compare_previous:
        period_days = (end - start).days + 1
        prev_start = start - timedelta(days=period_days)
        prev_end = start - timedelta(days=1)

        p_res = await db.execute(
            select(
                MetricDaily.date,
                func.coalesce(func.sum(MetricDaily.impressions), 0),
                func.coalesce(func.sum(MetricDaily.clicks), 0),
                func.coalesce(func.sum(MetricDaily.spend), 0.0),
                func.coalesce(func.sum(MetricDaily.sales), 0.0),
                func.coalesce(func.sum(MetricDaily.orders), 0),
            ).where(
                MetricDaily.entity_type == "campaign",
                MetricDaily.entity_id.in_(camp_ids),
                MetricDaily.date >= prev_start,
                MetricDaily.date <= prev_end
            ).group_by(MetricDaily.date).order_by(MetricDaily.date)
        )
        prev_date_map = {row[0]: row for row in p_res.all()}
        curr_d = prev_start
        while curr_d <= prev_end:
            row = prev_date_map.get(curr_d)
            if row:
                imp, clk, spd, sls, ord_cnt = int(row[1]), int(row[2]), round(float(row[3]), 2), round(float(row[4]), 2), int(row[5])
            else:
                imp, clk, spd, sls, ord_cnt = 0, 0, 0.0, 0.0, 0
            derived = derive(imp, clk, spd, sls, ord_cnt)
            previous_series.append({"date": str(curr_d), **derived})
            curr_d += timedelta(days=1)

    return {"series": series, "previous_series": previous_series}

@router.get("")
async def get_campaigns(
    ads_account_id: Optional[int] = None,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    q: Optional[str] = None,
    state: Optional[str] = None,
    delivery_status: Optional[str] = None,
    campaign_type: Optional[str] = None,
    targeting_type: Optional[str] = None,
    bidding_strategy: Optional[str] = None,
    portfolio: Optional[str] = None,
    min_spend: Optional[float] = None,
    max_spend: Optional[float] = None,
    min_sales: Optional[float] = None,
    max_sales: Optional[float] = None,
    min_acos: Optional[float] = None,
    max_acos: Optional[float] = None,
    min_roas: Optional[float] = None,
    min_clicks: Optional[int] = None,
    max_orders: Optional[int] = None,
    sort_by: str = "spend",
    sort_dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if ads_account_id is None:
        query = select(Campaign).join(AdsAccount).where(AdsAccount.user_id == current_user.id)
        result = await db.execute(query)
        return result.scalars().all()

    account = await assert_owned_account(db, ads_account_id, current_user)
    today = today_for(account.marketplace)
    end = end_date or today
    start = start_date or (end - timedelta(days=6))

    camp_q = select(Campaign).where(Campaign.ads_account_id == ads_account_id)
    if state: camp_q = camp_q.where(Campaign.state == state.upper())
    if delivery_status: camp_q = camp_q.where(Campaign.delivery_status == delivery_status.upper())
    if campaign_type: camp_q = camp_q.where(Campaign.campaign_type == campaign_type.upper())
    if targeting_type: camp_q = camp_q.where(Campaign.targeting_type == targeting_type.upper())
    if bidding_strategy: camp_q = camp_q.where(Campaign.bidding_strategy == bidding_strategy.upper())
    if portfolio:
        if portfolio.lower() == "uncategorized":
            camp_q = camp_q.where(Campaign.portfolio_name.is_(None))
        else:
            camp_q = camp_q.where(Campaign.portfolio_name == portfolio)

    all_camps = (await db.execute(camp_q)).scalars().all()

    if q and q.strip():
        q_clean = q.strip().lower()
        matched = []
        for c in all_camps:
            name_m = q_clean in (c.name or "").lower()
            id_m = (c.amazon_campaign_id and q_clean in c.amazon_campaign_id.lower()) or (c.external_id and q_clean in c.external_id.lower())
            prod_asins = (await db.execute(
                select(Product.asin).join(CampaignProduct, CampaignProduct.product_id == Product.id)
                .where(CampaignProduct.campaign_id == c.id)
            )).scalars().all()
            asin_m = any(q_clean in (asin or "").lower() for asin in prod_asins)
            if name_m or id_m or asin_m:
                matched.append(c)
        all_camps = matched

    camp_ids = [c.id for c in all_camps]

    metrics_map = {}
    if camp_ids:
        m_q = select(
            MetricDaily.entity_id,
            func.coalesce(func.sum(MetricDaily.impressions), 0),
            func.coalesce(func.sum(MetricDaily.clicks), 0),
            func.coalesce(func.sum(MetricDaily.spend), 0.0),
            func.coalesce(func.sum(MetricDaily.sales), 0.0),
            func.coalesce(func.sum(MetricDaily.orders), 0),
        ).where(
            MetricDaily.entity_type == "campaign",
            MetricDaily.entity_id.in_(camp_ids),
            MetricDaily.date >= start,
            MetricDaily.date <= end
        ).group_by(MetricDaily.entity_id)
        m_res = await db.execute(m_q)
        for row in m_res.all():
            metrics_map[row[0]] = {
                "impressions": int(row[1]),
                "clicks": int(row[2]),
                "spend": round(float(row[3]), 2),
                "sales": round(float(row[4]), 2),
                "orders": int(row[5])
            }

    today_spend_map = {}
    if camp_ids:
        t_res = await db.execute(
            select(
                MetricDaily.entity_id,
                func.coalesce(func.sum(MetricDaily.spend), 0.0)
            ).where(
                MetricDaily.entity_type == "campaign",
                MetricDaily.entity_id.in_(camp_ids),
                MetricDaily.date == today
            ).group_by(MetricDaily.entity_id)
        )
        for row in t_res.all():
            today_spend_map[row[0]] = round(float(row[1]), 2)

    period_days = (end - start).days + 1
    prev_start = start - timedelta(days=period_days)
    prev_end = start - timedelta(days=1)
    prev_metrics_map = {}
    if camp_ids:
        p_q = select(
            MetricDaily.entity_id,
            func.coalesce(func.sum(MetricDaily.impressions), 0),
            func.coalesce(func.sum(MetricDaily.clicks), 0),
            func.coalesce(func.sum(MetricDaily.spend), 0.0),
            func.coalesce(func.sum(MetricDaily.sales), 0.0),
            func.coalesce(func.sum(MetricDaily.orders), 0),
        ).where(
            MetricDaily.entity_type == "campaign",
            MetricDaily.entity_id.in_(camp_ids),
            MetricDaily.date >= prev_start,
            MetricDaily.date <= prev_end
        ).group_by(MetricDaily.entity_id)
        p_res = await db.execute(p_q)
        for row in p_res.all():
            prev_metrics_map[row[0]] = {
                "impressions": int(row[1]),
                "clicks": int(row[2]),
                "spend": round(float(row[3]), 2),
                "sales": round(float(row[4]), 2),
                "orders": int(row[5])
            }

    campaign_rows = []
    for c in all_camps:
        m = metrics_map.get(c.id, {"impressions": 0, "clicks": 0, "spend": 0.0, "sales": 0.0, "orders": 0})
        derived = derive(m["impressions"], m["clicks"], m["spend"], m["sales"], m["orders"])
        t_spd = today_spend_map.get(c.id, 0.0)
        daily_b = float(c.daily_budget or 100.0)
        util_pct = round((t_spd / daily_b) * 100.0, 1) if daily_b > 0 else 0.0

        top_pct = int(c.placement_top_of_search_pct or c.placement_top_pct or 0)
        prod_pct = int(c.placement_product_pages_pct or c.placement_product_page_pct or 0)
        rest_pct = int(c.placement_rest_of_search_pct or c.placement_rest_pct or 0)

        ag_default_bid = (await db.execute(
            select(AdGroup.default_bid).where(AdGroup.campaign_id == c.id).limit(1)
        )).scalar() or 15.0
        max_effective_bid = compute_max_effective_bid(ag_default_bid, top_pct, c.bidding_strategy, "TOP_OF_SEARCH")

        ag_count = (await db.execute(
            select(func.count(AdGroup.id)).where(AdGroup.campaign_id == c.id)
        )).scalar() or 0
        prod_count = (await db.execute(
            select(func.count(CampaignProduct.id)).where(CampaignProduct.campaign_id == c.id)
        )).scalar() or 0

        row = {
            "id": c.id,
            "amazon_campaign_id": c.amazon_campaign_id or c.external_id,
            "name": c.name,
            "state": c.state,
            "delivery_status": c.delivery_status,
            "campaign_type": c.campaign_type,
            "targeting_type": c.targeting_type,
            "bidding_strategy": c.bidding_strategy,
            "daily_budget": daily_b,
            "today_spend": t_spd,
            "budget_utilization_pct": util_pct,
            "portfolio_name": c.portfolio_name,
            "start_date": str(c.start_date) if c.start_date else None,
            "end_date": str(c.end_date) if c.end_date else None,
            "placement_top_of_search_pct": top_pct,
            "placement_product_pages_pct": prod_pct,
            "placement_rest_of_search_pct": rest_pct,
            "max_effective_top_of_search_bid": max_effective_bid,
            "ad_groups_count": ag_count,
            "products_count": prod_count,
            **derived
        }

        if min_spend is not None and row["spend"] < min_spend: continue
        if max_spend is not None and row["spend"] > max_spend: continue
        if min_sales is not None and row["sales"] < min_sales: continue
        if max_sales is not None and row["sales"] > max_sales: continue
        if min_roas is not None and row["roas"] < min_roas: continue
        if min_clicks is not None and row["clicks"] < min_clicks: continue
        if max_orders is not None and row["orders"] > max_orders: continue
        if min_acos is not None:
            if row["acos"] is None or row["acos"] < min_acos: continue
        if max_acos is not None:
            if row["acos"] is None or row["acos"] > max_acos: continue

        campaign_rows.append(row)

    sum_imp = sum(r["impressions"] for r in campaign_rows)
    sum_clk = sum(r["clicks"] for r in campaign_rows)
    sum_spd = round(sum(r["spend"] for r in campaign_rows), 2)
    sum_sls = round(sum(r["sales"] for r in campaign_rows), 2)
    sum_ord = sum(r["orders"] for r in campaign_rows)
    curr_summary = derive(sum_imp, sum_clk, sum_spd, sum_sls, sum_ord)

    prev_imp = sum(prev_metrics_map.get(r["id"], {}).get("impressions", 0) for r in campaign_rows)
    prev_clk = sum(prev_metrics_map.get(r["id"], {}).get("clicks", 0) for r in campaign_rows)
    prev_spd = round(sum(prev_metrics_map.get(r["id"], {}).get("spend", 0.0) for r in campaign_rows), 2)
    prev_sls = round(sum(prev_metrics_map.get(r["id"], {}).get("sales", 0.0) for r in campaign_rows), 2)
    prev_ord = sum(prev_metrics_map.get(r["id"], {}).get("orders", 0) for r in campaign_rows)
    prev_summary = derive(prev_imp, prev_clk, prev_spd, prev_sls, prev_ord)

    deltas = compute_period_deltas(curr_summary, prev_summary)
    summary = {
        "current": curr_summary,
        "previous": prev_summary,
        "deltas": deltas
    }

    reverse = (sort_dir.lower() == "desc")
    if sort_by == "acos":
        if reverse:
            campaign_rows.sort(
                key=lambda r: (2, r["spend"]) if r.get("acos_is_infinite") else ((1, r["acos"]) if r.get("acos") is not None else (0, 0)),
                reverse=True
            )
        else:
            campaign_rows.sort(
                key=lambda r: (2, 0) if (r["spend"] == 0 and r["sales"] == 0) else ((1, r["spend"]) if r.get("acos_is_infinite") else (0, r.get("acos") or 0.0)),
                reverse=False
            )
    else:
        def _get_sort_val(r):
            v = r.get(sort_by)
            if v is None:
                return -1e9 if reverse else 1e9
            return v
        campaign_rows.sort(key=_get_sort_val, reverse=reverse)

    total_count = len(campaign_rows)
    start_idx = (page - 1) * page_size
    paged_items = campaign_rows[start_idx : start_idx + page_size]

    return {
        "items": paged_items,
        "summary": summary,
        "total_count": total_count,
        "page": page,
        "page_size": page_size
    }

@router.get("/report")
async def report(
    ads_account_id: int,
    start: Optional[date] = None,
    end: Optional[date] = None,
    q: Optional[str] = None,
    state: List[str] = Query(default=None),
    targeting_type: Optional[str] = None,
    health: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    min_acos: Optional[float] = None,
    max_acos: Optional[float] = None,
    min_spend: Optional[float] = None,
    max_spend: Optional[float] = None,
    min_sales: Optional[float] = None,
    max_sales: Optional[float] = None,
    min_orders: Optional[float] = None,
    max_orders: Optional[float] = None,
    min_clicks: Optional[float] = None,
    max_clicks: Optional[float] = None,
    min_impressions: Optional[float] = None,
    max_impressions: Optional[float] = None,
    min_roas: Optional[float] = None,
    max_roas: Optional[float] = None,
    min_cpc: Optional[float] = None,
    max_cpc: Optional[float] = None,
    min_ctr: Optional[float] = None,
    max_ctr: Optional[float] = None,
    min_utilization: Optional[float] = None,
    max_utilization: Optional[float] = None,
    sort: str = "spend",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    export: bool = False,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    today = today_for(account.marketplace)
    end_date = end or today
    start_date = start or (end_date - timedelta(days=7))

    metric_filters = {
        "min_spend": min_spend, "max_spend": max_spend,
        "min_sales": min_sales, "max_sales": max_sales,
        "min_orders": min_orders, "max_orders": max_orders,
        "min_clicks": min_clicks, "max_clicks": max_clicks,
        "min_impressions": min_impressions, "max_impressions": max_impressions,
        "min_roas": min_roas, "max_roas": max_roas,
        "min_cpc": min_cpc, "max_cpc": max_cpc,
        "min_ctr": min_ctr, "max_ctr": max_ctr,
        "min_utilization": min_utilization, "max_utilization": max_utilization,
    }
    # Clean none entries
    metric_filters = {k: v for k, v in metric_filters.items() if v is not None}

    return await get_report(
        db, account, start_date, end_date, q, state, targeting_type, health,
        min_budget, max_budget, min_acos, max_acos, metric_filters,
        sort, dir, page, page_size, export
    )

@router.get("/summary")
async def summary(
    ads_account_id: int,
    start: Optional[date] = None,
    end: Optional[date] = None,
    granularity: str = "day",
    compare: bool = False,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    today = today_for(account.marketplace)
    end_date = end or today
    start_date = start or (end_date - timedelta(days=7))
    
    return await get_summary(db, account, start_date, end_date, granularity, compare=compare)

@router.get("/alerts")
async def alerts(
    ads_account_id: int,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    today = today_for(account.marketplace)
    start_date = today - timedelta(days=30)
    
    settings = (await db.execute(select(UserCampaignSettings).where(UserCampaignSettings.user_id == current_user.id))).scalars().first()
    if not settings:
        settings = UserCampaignSettings(user_id=current_user.id, acos_low=20.0, acos_high=90.0, default_target_acos=30.0)
        db.add(settings)
        await db.commit()
    
    rep = await get_report(db, account, start_date, today, page_size=1000)
    
    breaches = []
    low = settings.acos_low
    high = settings.acos_high
    
    for item in rep["items"]:
        spend = item["spend"]
        sales = item["sales"]
        acos = item["acos"]
        
        # Campaigns with no sales are excluded from lower-than
        if acos is not None:
            if acos < low:
                breaches.append(item)
            elif acos > high:
                breaches.append(item)
        elif spend > 0:
            # Spend > 0 with no sales is a higher-than breach
            breaches.append(item)
            
    return breaches

@router.get("/suggestions")
async def suggestions(
    ads_account_id: int,
    product_ids: str = "",
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    rules = marketplace_or_us(account.marketplace)
    
    pids = []
    if product_ids:
        try:
            pids = [int(p.strip()) for p in product_ids.split(",") if p.strip()]
        except ValueError:
            pass
            
    today = today_for(account.marketplace)
    start_date = today - timedelta(days=30)
    
    query = (
        select(func.sum(MetricDaily.spend), func.sum(MetricDaily.clicks))
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .where(Campaign.ads_account_id == account.id, MetricDaily.entity_type == "campaign", MetricDaily.date >= start_date)
    )
    if pids:
        query = query.join(CampaignProduct, CampaignProduct.campaign_id == Campaign.id).where(CampaignProduct.product_id.in_(pids))
        
    res = (await db.execute(query)).first()
    spend, clicks = res[0] or 0.0, res[1] or 0
    
    if clicks >= 10:
        cpc = spend / clicks
        basis = "history"
    else:
        cpc = rules["recommended_budget"] * 0.05
        basis = "default"
        
    default_bid = round(max(rules["min_bid"], min(rules["max_bid"], cpc)), 2)
    sugg_budget = round(max(rules["min_budget"], default_bid * 15), 2)
            
    return {
        "budget": {
            "min": rules["min_budget"],
            "recommended": rules["recommended_budget"],
            "suggested": sugg_budget,
            "max": sugg_budget * 5
        },
        "default_bid": default_bid,
        "basis": basis
    }

@router.post("/bid-suggestions")
async def bid_suggestions(
    payload: BidSuggestionsRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    rules = marketplace_or_us(account.marketplace)
    default_bid = rules["recommended_budget"] * 0.05

    items = []
    for kw in payload.keywords:
        sug = MockAdsProvider.suggest_bid(kw.keyword_text, default_bid, rules)
        items.append({
            "keyword_text": kw.keyword_text,
            "match_type": kw.match_type,
            "low": sug["low"],
            "median": sug["median"],
            "high": sug["high"]
        })

    auto_groups = {
        "CLOSE_MATCH": MockAdsProvider.suggest_bid("close_match", default_bid, rules),
        "LOOSE_MATCH": MockAdsProvider.suggest_bid("loose_match", default_bid * 0.8, rules),
        "SUBSTITUTES": MockAdsProvider.suggest_bid("substitutes", default_bid * 0.9, rules),
        "COMPLEMENTS": MockAdsProvider.suggest_bid("complements", default_bid * 0.7, rules),
    }

    return {
        "items": items,
        "auto_groups": auto_groups
    }

@router.post("/forecast")
async def forecast(
    payload: ForecastRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    rules = marketplace_or_in(account.marketplace)

    # Return empty ranges when products, targets or bid are missing
    if not payload.product_ids or payload.daily_budget is None or payload.default_bid is None or payload.targeting_type is None or (payload.targeting_type == "MANUAL" and not payload.target_count):
        return {
            "period": "weekly",
            "clicks": None,
            "orders": None,
            "spend": None,
            "basis": "default",
            "is_estimate": True,
            "est_daily_impressions": [0, 0],
            "est_daily_clicks": [0, 0],
            "est_daily_spend": [0.0, 0.0],
            "est_daily_orders": [0, 0],
            "est_acos_pct": 0.0,
            "max_effective_cpc": 0.0,
            "warnings": []
        }

    today = today_for(account.marketplace)
    start_30 = today - timedelta(days=30)

    hist_res = (await db.execute(
        select(
            func.sum(MetricDaily.spend).label("spend"),
            func.sum(MetricDaily.clicks).label("clicks"),
            func.sum(MetricDaily.orders).label("orders")
        )
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= start_30
        )
    )).first()

    tot_spend = hist_res.spend or 0.0
    tot_clicks = hist_res.clicks or 0
    tot_orders = hist_res.orders or 0

    has_history = tot_clicks >= 10
    history_cpc = (tot_spend / tot_clicks) if has_history else None
    history_cvr = (tot_orders / tot_clicks) if (has_history and tot_orders > 0) else None

    default_bid = float(payload.default_bid)
    daily_budget = float(payload.daily_budget)
    target_count = payload.target_count or 1
    targeting_type = payload.targeting_type

    cpc = max(history_cpc if has_history else default_bid * 0.8, rules["min_bid"])
    cvr = history_cvr if has_history else 0.08
    demand = (150 if targeting_type == "AUTO" else 30 * max(1, target_count) ** 0.5) * min(2.0, max(0.5, default_bid / cpc))
    budget_clicks = (daily_budget * 7) / cpc
    clicks = min(demand, budget_clicks)
    spend = min(clicks * cpc, daily_budget * 7)
    orders = clicks * cvr

    clicks_low = round(clicks * 0.7)
    clicks_high = round(clicks * 1.3)
    orders_low = round(orders * 0.6)
    orders_high = round(orders * 1.4)
    spend_low = round(min(spend * 0.7, daily_budget * 7), 2)
    spend_high = round(min(spend * 1.3, daily_budget * 7), 2)

    # Daily estimates
    daily_clicks_val = clicks / 7.0
    daily_spend_val = spend / 7.0
    daily_orders_val = orders / 7.0
    est_daily_imp = (daily_clicks_val / 0.015) if daily_clicks_val > 0 else 0

    top_pct = payload.placement_top_of_search_pct or 0
    strategy = payload.bidding_strategy or "DOWN_ONLY"
    max_effective_cpc = compute_max_effective_bid(default_bid, top_pct, strategy, "TOP_OF_SEARCH")

    warnings = []
    if daily_budget < rules["min_daily_budget"]:
        warnings.append(f"Daily budget is below the recommended minimum of ₹{rules['min_daily_budget']:,.2f}.")
    if max_effective_cpc > daily_budget * 0.5:
        warnings.append(f"Max effective CPC (₹{max_effective_cpc:,.2f}) may exhaust daily budget rapidly.")

    est_sales_val = daily_orders_val * 850.0
    est_acos = round((daily_spend_val / est_sales_val) * 100.0, 1) if est_sales_val > 0 else 24.5

    return {
        "period": "weekly",
        "clicks": {"low": clicks_low, "high": clicks_high},
        "orders": {"low": orders_low, "high": orders_high},
        "spend": {"low": spend_low, "high": spend_high},
        "basis": "history" if has_history else "default",
        "is_estimate": True,
        "est_daily_impressions": [int(round(est_daily_imp * 0.7)), int(round(est_daily_imp * 1.3))],
        "est_daily_clicks": [int(round(daily_clicks_val * 0.7)), int(round(daily_clicks_val * 1.3))],
        "est_daily_spend": [round(min(daily_spend_val * 0.7, daily_budget), 2), round(min(daily_spend_val * 1.3, daily_budget), 2)],
        "est_daily_orders": [round(daily_orders_val * 0.6, 1), round(daily_orders_val * 1.4, 1)],
        "est_acos_pct": est_acos,
        "max_effective_cpc": max_effective_cpc,
        "warnings": warnings
    }

@router.post("/wizard")
async def launch_wizard_campaign(
    payload: CampaignWizardIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    rules = marketplace_or_in(account.marketplace)

    clean_name = payload.name.strip()
    dup = (await db.execute(
        select(Campaign.id).where(
            Campaign.ads_account_id == account.id,
            func.lower(func.trim(Campaign.name)) == clean_name.lower()
        )
    )).scalar()
    if dup:
        raise HTTPException(status_code=409, detail=f"A campaign named '{clean_name}' already exists")

    if payload.daily_budget < rules["min_daily_budget"] or payload.daily_budget > rules["max_daily_budget"]:
        raise HTTPException(
            status_code=422,
            detail=[{"field": "daily_budget", "message": f"Daily budget must be between ₹{rules['min_daily_budget']:,.2f} and ₹{rules['max_daily_budget']:,.2f}"}]
        )

    if payload.end_date and payload.start_date > payload.end_date:
        raise HTTPException(status_code=422, detail=[{"field": "end_date", "message": "End date cannot be earlier than start date"}])

    top_pct = max(0, min(900, int(payload.placement_top_of_search_pct or payload.placement_top_pct or 0)))
    prod_pct = max(0, min(900, int(payload.placement_product_pages_pct or payload.placement_product_page_pct or 0)))
    rest_pct = max(0, min(900, int(payload.placement_rest_of_search_pct or payload.placement_rest_pct or 0)))

    # Conflict validation: No positive EXACT keyword may conflict with NEGATIVE_EXACT
    pos_exacts = set()
    for ag_in in payload.ad_groups:
        for kw_in in ag_in.keywords:
            if kw_in.match_type == "EXACT":
                pos_exacts.add(clean_keyword_text(kw_in.keyword_text))

    for neg_in in payload.negative_keywords:
        if neg_in.match_type == "NEGATIVE_EXACT":
            cleaned_neg = clean_keyword_text(neg_in.keyword_text)
            if cleaned_neg in pos_exacts:
                raise HTTPException(
                    status_code=422,
                    detail=[{"field": "negative_keywords", "message": f"Negative exact keyword '{cleaned_neg}' conflicts with an active positive exact keyword"}]
                )

    c = Campaign(
        ads_account_id=account.id,
        amazon_campaign_id=f"amzn_camp_{account.id}_{uuid.uuid4().hex[:8]}",
        external_id=f"ext_camp_{uuid.uuid4().hex[:8]}",
        name=clean_name,
        state=payload.state,
        delivery_status="DELIVERING" if payload.state == "ENABLED" else payload.state,
        campaign_type=payload.campaign_type,
        targeting_type=payload.targeting_type,
        bidding_strategy=payload.bidding_strategy,
        daily_budget=payload.daily_budget,
        target_acos=payload.target_acos,
        start_date=payload.start_date,
        end_date=payload.end_date,
        placement_top_of_search_pct=top_pct,
        placement_product_pages_pct=prod_pct,
        placement_rest_of_search_pct=rest_pct,
        placement_top_pct=top_pct,
        placement_product_page_pct=prod_pct,
        placement_rest_pct=rest_pct,
        portfolio_name=payload.portfolio_name,
        site=payload.site or "IN",
        negative_brands=list(payload.negative_brands or [])
    )
    db.add(c)
    await db.flush()

    for pid in payload.product_ids:
        cp = CampaignProduct(campaign_id=c.id, product_id=pid, state="ENABLED")
        db.add(cp)

    ad_groups_data = payload.ad_groups or [AdGroupIn(name=f"{clean_name} Ad Group", default_bid=rules["default_bid"])]
    for ag_in in ad_groups_data:
        ag = AdGroup(campaign_id=c.id, name=ag_in.name, default_bid=ag_in.default_bid, state="ENABLED")
        db.add(ag)
        await db.flush()

        if payload.targeting_type == "AUTO":
            auto_keys = ["CLOSE_MATCH", "LOOSE_MATCH", "SUBSTITUTES", "COMPLEMENTS"]
            target_bids = {t.value: t.bid for t in ag_in.targets if t.kind == "AUTO_GROUP"}
            for k in auto_keys:
                b_val = target_bids.get(k) or ag_in.default_bid
                tg = Target(
                    campaign_id=c.id,
                    ad_group_id=ag.id,
                    target_type="AUTO_GROUP",
                    expression_type=k,
                    expression_value=k,
                    kind="AUTO_GROUP",
                    value=k,
                    bid=b_val,
                    state="ENABLED"
                )
                db.add(tg)
        else:
            for kw_in in ag_in.keywords:
                kw = Keyword(
                    ad_group_id=ag.id,
                    keyword_text=clean_keyword_text(kw_in.keyword_text),
                    match_type=kw_in.match_type,
                    bid=kw_in.bid or ag_in.default_bid,
                    state="ENABLED"
                )
                db.add(kw)
            for t_in in ag_in.targets:
                tg = Target(
                    campaign_id=c.id,
                    ad_group_id=ag.id,
                    target_type=t_in.kind,
                    expression_type="ASIN_SAME_AS" if t_in.kind == "ASIN" else "CATEGORY_SAME_AS",
                    expression_value=t_in.value,
                    kind=t_in.kind,
                    value=t_in.value,
                    bid=t_in.bid or ag_in.default_bid,
                    state="ENABLED"
                )
                db.add(tg)

    for neg_in in payload.negative_keywords:
        nk = NegativeKeyword(
            campaign_id=c.id,
            keyword_text=clean_keyword_text(neg_in.keyword_text),
            match_type=neg_in.match_type
        )
        db.add(nk)

    for asin in payload.negative_asins:
        clean_asin = asin.strip().upper()
        nk = NegativeKeyword(
            campaign_id=c.id,
            keyword_text=clean_asin,
            match_type="NEGATIVE_EXACT"
        )
        db.add(nk)

    if payload.draft_id:
        draft = (await db.execute(
            select(CampaignDraft).where(CampaignDraft.id == payload.draft_id, CampaignDraft.user_id == current_user.id)
        )).scalars().first()
        if draft:
            await db.delete(draft)

    hist = CampaignHistory(
        campaign_id=c.id,
        event_type="CAMPAIGN_LAUNCHED",
        event_title="Campaign Launched via Wizard",
        description=f"Campaign '{c.name}' launched with ₹{c.daily_budget:,.2f} daily budget.",
        new_value=f"₹{c.daily_budget:,.2f}",
        user_id=current_user.id
    )
    db.add(hist)

    await db.commit()
    await db.refresh(c)
    return {
        "id": c.id,
        "name": c.name,
        "state": c.state,
        "status_label": "Delivering" if c.state == "ENABLED" else "Paused",
        "campaign": {
            "id": c.id,
            "name": c.name,
            "state": c.state,
            "daily_budget": c.daily_budget,
            "delivery_status": c.delivery_status
        }
    }

@router.get("/negative-suggestions")
async def negative_suggestions(
    ads_account_id: int,
    product_ids: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    today = today_for(account.marketplace)
    start_60 = today - timedelta(days=60)

    query = (
        select(
            SearchTerm.search_term.label("keyword_text"),
            func.sum(SearchTerm.clicks).label("clicks"),
            func.sum(SearchTerm.spend).label("spend")
        )
        .join(Campaign, SearchTerm.campaign_id == Campaign.id)
        .where(
            Campaign.ads_account_id == account.id,
            SearchTerm.date >= start_60
        )
        .group_by(SearchTerm.search_term)
        .having(
            func.sum(SearchTerm.clicks) >= 10,
            func.sum(SearchTerm.sales) == 0
        )
        .order_by(func.sum(SearchTerm.spend).desc())
        .limit(20)
    )

    rows = (await db.execute(query)).all()
    return [
        {
            "keyword_text": r.keyword_text,
            "clicks": int(r.clicks or 0),
            "spend": round(float(r.spend or 0.0), 2),
            "reason": "10+ clicks, no sales"
        }
        for r in rows
    ]

@router.get("/brands")
async def get_brands(
    q: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    provider = MockAdsProvider(db)
    return provider.search_brands(q)

@router.get("/name-check")
async def check_name(
    ads_account_id: int,
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, ads_account_id, current_user)
    clean_name = name.strip().lower()
    existing = (await db.execute(
        select(Campaign.id).where(
            Campaign.ads_account_id == account.id,
            func.lower(Campaign.name) == clean_name
        )
    )).scalars().first()
    return {"available": existing is None}

@router.get("/settings/campaigns", response_model=UserCampaignSettingsResponse)
async def get_campaign_settings(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    settings = (await db.execute(select(UserCampaignSettings).where(UserCampaignSettings.user_id == current_user.id))).scalars().first()
    if not settings:
        settings = UserCampaignSettings(user_id=current_user.id, acos_low=20.0, acos_high=90.0, default_target_acos=30.0)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)

    rules = settings.health_rules if settings.health_rules else get_default_health_rules(settings.acos_high)
    return {
        "acos_low": settings.acos_low,
        "acos_high": settings.acos_high,
        "default_target_acos": settings.default_target_acos,
        "bid_limit_acos": settings.bid_limit_acos,
        "health_rules": rules
    }

@router.put("/settings/campaigns")
async def update_campaign_settings(
    settings_in: UserCampaignSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    settings = (await db.execute(select(UserCampaignSettings).where(UserCampaignSettings.user_id == current_user.id))).scalars().first()
    if not settings:
        settings = UserCampaignSettings(user_id=current_user.id)
        db.add(settings)

    settings.acos_low = settings_in.acos_low
    settings.acos_high = settings_in.acos_high
    settings.default_target_acos = settings_in.default_target_acos
    settings.bid_limit_acos = settings_in.bid_limit_acos
    if settings_in.health_rules is not None:
        settings.health_rules = [r.model_dump() for r in settings_in.health_rules]

    await db.commit()
    await db.refresh(settings)
    return {
        "status": "ok",
        "acos_low": settings.acos_low,
        "acos_high": settings.acos_high,
        "default_target_acos": settings.default_target_acos,
        "bid_limit_acos": settings.bid_limit_acos,
        "health_rules": settings.health_rules or get_default_health_rules(settings.acos_high)
    }

@router.post("/validate")
async def validate(
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    rules = marketplace_or_us(account.marketplace)
    errors, warnings = await validate_create(db, account, rules, payload)
    return {"errors": errors, "warnings": warnings}

@router.post("/bulk")
async def bulk_action(
    payload: BulkCampaignActionIn,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    campaign_ids = payload.campaign_ids
    results = []
    updated_cnt = 0
    skipped_cnt = 0
    failed_cnt = 0

    min_daily_budget = 100.0
    max_daily_budget = 20000000.0

    floor = max(min_daily_budget, float(payload.min_budget_floor if payload.min_budget_floor is not None else min_daily_budget))
    cap = min(max_daily_budget, float(payload.max_budget_cap if payload.max_budget_cap is not None else max_daily_budget))

    for cid in campaign_ids:
        async with db.begin_nested():
            try:
                c = await load_owned_campaign(db, cid, current_user)
                if c.state == "ARCHIVED" and payload.action != "ARCHIVE":
                    skipped_cnt += 1
                    results.append({
                        "id": cid,
                        "status": "skipped",
                        "ok": False,
                        "reason": "Campaign is archived",
                        "error": "Cannot enable archived campaign",
                        "state": c.state,
                        "daily_budget": c.daily_budget
                    })
                    continue

                account = (await db.execute(select(AdsAccount).where(AdsAccount.id == c.ads_account_id))).scalars().first()
                today = today_for(account.marketplace) if account else date.today()

                if payload.action == "ENABLE":
                    c.state = "ENABLED"
                    c.delivery_status = "DELIVERING"
                elif payload.action == "PAUSE":
                    c.state = "PAUSED"
                    c.delivery_status = "PAUSED"
                elif payload.action == "ARCHIVE":
                    c.state = "ARCHIVED"
                    c.delivery_status = "ARCHIVED"
                elif payload.action in ("BUDGET_SET", "SET_BUDGET"):
                    val = float(payload.value or 0.0)
                    c.daily_budget = round(max(floor, min(cap, val)), 2)
                elif payload.action in ("BUDGET_INCREASE_PCT", "CHANGE_BUDGET_PCT"):
                    pct_val = float(payload.value or 0.0)
                    new_b = c.daily_budget * (1.0 + pct_val / 100.0)
                    c.daily_budget = round(max(floor, min(cap, new_b)), 2)
                elif payload.action == "BUDGET_DECREASE_PCT":
                    pct_val = float(payload.value or 0.0)
                    new_b = c.daily_budget * (1.0 - pct_val / 100.0)
                    c.daily_budget = round(max(floor, min(cap, new_b)), 2)
                elif payload.action == "BUDGET_INCREASE_AMT":
                    amt_val = float(payload.value or 0.0)
                    new_b = c.daily_budget + amt_val
                    c.daily_budget = round(max(floor, min(cap, new_b)), 2)
                elif payload.action == "BUDGET_DECREASE_AMT":
                    amt_val = float(payload.value or 0.0)
                    new_b = c.daily_budget - amt_val
                    c.daily_budget = round(max(floor, min(cap, new_b)), 2)
                elif payload.action == "SET_BIDDING_STRATEGY":
                    c.bidding_strategy = str(payload.value)
                elif payload.action == "SET_PLACEMENT_PCT":
                    val_pct = max(0, min(900, int(payload.value or 0)))
                    plc = (payload.placement or "TOP_OF_SEARCH").upper()
                    if plc in ("TOP_OF_SEARCH", "TOP_PCT"):
                        c.placement_top_of_search_pct = val_pct
                        c.placement_top_pct = val_pct
                    elif plc in ("PRODUCT_PAGES", "PRODUCT_PAGE_PCT"):
                        c.placement_product_pages_pct = val_pct
                        c.placement_product_page_pct = val_pct
                    elif plc in ("REST_OF_SEARCH", "REST_PCT"):
                        c.placement_rest_of_search_pct = val_pct
                        c.placement_rest_pct = val_pct
                elif payload.action == "SET_PORTFOLIO":
                    c.portfolio_name = str(payload.value) if payload.value else None

                if c.state == "ENABLED":
                    t_spend = (await db.execute(
                        select(func.sum(MetricDaily.spend))
                        .where(MetricDaily.entity_type == "campaign", MetricDaily.entity_id == c.id, MetricDaily.date == today)
                    )).scalar() or 0.0
                    if t_spend >= c.daily_budget:
                        c.delivery_status = "OUT_OF_BUDGET"
                    elif c.end_date and c.end_date < today:
                        c.delivery_status = "ENDED"
                    else:
                        c.delivery_status = "DELIVERING"

                updated_cnt += 1
                results.append({
                    "id": cid,
                    "status": "updated",
                    "ok": True,
                    "state": c.state,
                    "daily_budget": c.daily_budget
                })
            except Exception as e:
                failed_cnt += 1
                results.append({
                    "id": cid,
                    "status": "failed",
                    "ok": False,
                    "reason": str(e)
                })

    await db.commit()
    return {
        "updated": updated_cnt,
        "skipped": skipped_cnt,
        "failed": failed_cnt,
        "results": results
    }

@router.post("")
async def create(
    payload: CampaignCreate,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    account = await assert_owned_account(db, payload.ads_account_id, current_user)
    return await create_campaign(db, account, payload)

# ----------------- Dynamic /{campaign_id} routes -----------------

@router.get("/{campaign_id}")
async def get_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    
    ag_res = await db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id))
    ad_groups = ag_res.scalars().all()
    
    cp_res = await db.execute(select(Product).join(CampaignProduct).where(CampaignProduct.campaign_id == c.id))
    products = cp_res.scalars().all()
    
    nk_res = await db.execute(select(NegativeKeyword).where(NegativeKeyword.campaign_id == c.id))
    negatives = nk_res.scalars().all()
    
    res = {
        "id": c.id,
        "ads_account_id": c.ads_account_id,
        "name": c.name,
        "state": c.state,
        "delivery_status": c.delivery_status,
        "targeting_type": c.targeting_type,
        "campaign_type": c.campaign_type,
        "portfolio_name": c.portfolio_name,
        "site": c.site,
        "start_date": c.start_date,
        "end_date": c.end_date,
        "daily_budget": c.daily_budget,
        "target_acos": c.target_acos,
        "bidding_strategy": c.bidding_strategy,
        "placement_top_of_search_pct": c.placement_top_of_search_pct or c.placement_top_pct,
        "placement_product_pages_pct": c.placement_product_pages_pct or c.placement_product_page_pct,
        "placement_rest_of_search_pct": c.placement_rest_of_search_pct or c.placement_rest_pct,
        "placement_top_pct": c.placement_top_pct,
        "placement_rest_pct": c.placement_rest_pct,
        "placement_product_page_pct": c.placement_product_page_pct,
        "ad_groups": ad_groups,
        "products": products,
        "negative_keywords": negatives
    }
    return res

@router.patch("/{campaign_id}")
async def update_campaign(
    campaign_id: int,
    payload: CampaignUpdate,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == c.ads_account_id))).scalars().first()
    rules = marketplace_or_in(account.marketplace) if account else MARKETPLACE_RULES["IN"]
    today = today_for(account.marketplace) if account else date.today()

    if c.state == "ARCHIVED":
        raise HTTPException(
            status_code=409,
            detail="Archived campaigns cannot be modified"
        )

    if payload.name is not None:
        trimmed = payload.name.strip()
        dup = (await db.execute(
            select(Campaign.id).where(
                Campaign.ads_account_id == c.ads_account_id,
                Campaign.id != c.id,
                func.lower(func.trim(Campaign.name)) == trimmed.lower()
            )
        )).scalar()
        if dup:
            raise HTTPException(
                status_code=409,
                detail=f"A campaign named '{trimmed}' already exists"
            )
        c.name = trimmed

    if payload.daily_budget is not None:
        min_b = rules["min_daily_budget"]
        max_b = rules["max_daily_budget"]
        b_val = float(payload.daily_budget)
        if b_val < min_b or b_val > max_b:
            raise HTTPException(
                status_code=422,
                detail=[{
                    "field": "daily_budget",
                    "message": f"Daily budget must be between ₹{min_b:,.2f} and ₹{max_b:,.2f}"
                }]
            )
        c.daily_budget = b_val

    if payload.placement_top_of_search_pct is not None:
        val = int(payload.placement_top_of_search_pct)
        if val < 0 or val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "placement_top_of_search_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_top_of_search_pct = val
        c.placement_top_pct = val
    elif payload.placement_top_pct is not None:
        val = int(payload.placement_top_pct)
        if val < 0 or val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "placement_top_of_search_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_top_of_search_pct = val
        c.placement_top_pct = val

    if payload.placement_product_pages_pct is not None:
        val = int(payload.placement_product_pages_pct)
        if val < 0 or val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "placement_product_pages_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_product_pages_pct = val
        c.placement_product_page_pct = val
    elif payload.placement_product_page_pct is not None:
        val = int(payload.placement_product_page_pct)
        if val < 0 or val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "placement_product_pages_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_product_pages_pct = val
        c.placement_product_page_pct = val

    if payload.placement_rest_of_search_pct is not None:
        val = int(payload.placement_rest_of_search_pct)
        if val < 0 or val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "placement_rest_of_search_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_rest_of_search_pct = val
        c.placement_rest_pct = val
    elif payload.placement_rest_pct is not None:
        val = int(payload.placement_rest_pct)
        if val < 0 or val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "placement_rest_of_search_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_rest_of_search_pct = val
        c.placement_rest_pct = val

    new_start = payload.start_date if payload.start_date is not None else c.start_date
    new_end = payload.end_date if payload.end_date is not None else c.end_date
    if new_end and new_start and new_end < new_start:
        raise HTTPException(status_code=422, detail=[{"field": "end_date", "message": "End date cannot be earlier than start date"}])

    if payload.start_date is not None:
        c.start_date = payload.start_date
    if payload.end_date is not None:
        c.end_date = payload.end_date

    if payload.state is not None:
        c.state = payload.state
    if payload.bidding_strategy is not None:
        c.bidding_strategy = payload.bidding_strategy
    if payload.portfolio_name is not None:
        c.portfolio_name = payload.portfolio_name
    if payload.negative_brands is not None:
        c.negative_brands = payload.negative_brands

    t_spd = (await db.execute(
        select(func.sum(MetricDaily.spend))
        .where(MetricDaily.entity_type == "campaign", MetricDaily.entity_id == c.id, MetricDaily.date == today)
    )).scalar() or 0.0

    if c.state == "ARCHIVED":
        c.delivery_status = "ARCHIVED"
    elif c.state == "PAUSED":
        c.delivery_status = "PAUSED"
    elif c.end_date and c.end_date < today:
        c.delivery_status = "ENDED"
    elif t_spd >= c.daily_budget:
        c.delivery_status = "OUT_OF_BUDGET"
    else:
        c.delivery_status = "DELIVERING"

    await db.commit()
    await db.refresh(c)
    return c

@router.put("/{campaign_id}/state")
async def update_campaign_state(
    campaign_id: int,
    payload: StateUpdate,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED" and payload.state in ("ENABLED", "PAUSED"):
        raise HTTPException(400, "Cannot change state of archived campaign")
    c.state = payload.state
    await db.commit()
    await db.refresh(c)
    return c

@router.post("/{campaign_id}/duplicate")
async def duplicate_campaign(
    campaign_id: int,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == c.ads_account_id))).scalars().first()
    today = today_for(account.marketplace) if account else date.today()

    new_c = Campaign(
        ads_account_id=c.ads_account_id,
        amazon_campaign_id=f"{c.amazon_campaign_id or c.id}_copy",
        external_id=f"{c.external_id or c.id}_copy",
        name=f"{c.name} (Copy)",
        state="PAUSED",
        delivery_status="PAUSED",
        targeting_type=c.targeting_type,
        campaign_type=c.campaign_type,
        portfolio_name=c.portfolio_name,
        site=c.site,
        start_date=today,
        end_date=None,
        daily_budget=c.daily_budget,
        target_acos=c.target_acos,
        bidding_strategy=c.bidding_strategy,
        placement_top_of_search_pct=c.placement_top_of_search_pct or c.placement_top_pct,
        placement_product_pages_pct=c.placement_product_pages_pct or c.placement_product_page_pct,
        placement_rest_of_search_pct=c.placement_rest_of_search_pct or c.placement_rest_pct,
        placement_top_pct=c.placement_top_pct,
        placement_rest_pct=c.placement_rest_pct,
        placement_product_page_pct=c.placement_product_page_pct,
        negative_brands=list(c.negative_brands or []),
        product_id=c.product_id
    )
    db.add(new_c)
    await db.flush()

    prods = (await db.execute(select(CampaignProduct).where(CampaignProduct.campaign_id == c.id))).scalars().all()
    for p in prods:
        new_p = CampaignProduct(campaign_id=new_c.id, product_id=p.product_id, state=p.state)
        db.add(new_p)

    ags = (await db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id))).scalars().all()
    for ag in ags:
        new_ag = AdGroup(campaign_id=new_c.id, name=ag.name, default_bid=ag.default_bid)
        db.add(new_ag)
        await db.flush()

        kws = (await db.execute(select(Keyword).where(Keyword.ad_group_id == ag.id))).scalars().all()
        for kw in kws:
            new_kw = Keyword(ad_group_id=new_ag.id, keyword_text=kw.keyword_text, match_type=kw.match_type, bid=kw.bid, state=kw.state)
            db.add(new_kw)

        tgs = (await db.execute(select(Target).where(Target.ad_group_id == ag.id))).scalars().all()
        for tg in tgs:
            new_tg = Target(
                campaign_id=new_c.id,
                ad_group_id=new_ag.id,
                target_type=tg.target_type or tg.kind,
                expression_type=tg.expression_type,
                expression_value=tg.expression_value or tg.value,
                kind=tg.kind,
                value=tg.value,
                bid=tg.bid,
                state=tg.state
            )
            db.add(new_tg)

    negs = (await db.execute(select(NegativeKeyword).where(NegativeKeyword.campaign_id == c.id))).scalars().all()
    for nk in negs:
        new_nk = NegativeKeyword(campaign_id=new_c.id, keyword_text=nk.keyword_text, match_type=nk.match_type)
        db.add(new_nk)

    await db.commit()
    await db.refresh(new_c)
    return {"status": "ok", "id": new_c.id}

@router.get("/{campaign_id}/ad-groups")
async def get_campaign_ad_groups(
    campaign_id: int,
    start: Optional[date] = None,
    end: Optional[date] = None,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == c.ads_account_id))).scalars().first()
    today = today_for(account.marketplace)
    end_date = end or today
    start_date = start or (end_date - timedelta(days=7))
    
    ag_res = await db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id))
    ad_groups = ag_res.scalars().all()
    
    metric_sq = (
        select(
            MetricDaily.entity_id.label("ag_id"),
            func.sum(MetricDaily.impressions).label("impressions"),
            func.sum(MetricDaily.clicks).label("clicks"),
            func.sum(MetricDaily.spend).label("spend"),
            func.sum(MetricDaily.sales).label("sales"),
            func.sum(MetricDaily.orders).label("orders"),
            func.sum(MetricDaily.organic_sales).label("organic_sales")
        )
        .where(MetricDaily.entity_type == "ad_group", MetricDaily.date >= start_date, MetricDaily.date <= end_date)
        .group_by(MetricDaily.entity_id)
        .subquery()
    )
    
    res = []
    for ag in ad_groups:
        row = (await db.execute(select(metric_sq).where(metric_sq.c.ag_id == ag.id))).first()
        m = {
            "impressions": row[1] if row else 0, "clicks": row[2] if row else 0,
            "spend": row[3] if row else 0.0, "sales": row[4] if row else 0.0,
            "orders": row[5] if row else 0, "organic_sales": row[6] if row else 0.0
        }
        res.append({
            "id": ag.id,
            "campaign_id": ag.campaign_id,
            "name": ag.name,
            "state": ag.state,
            "default_bid": ag.default_bid,
            **derive(m)
        })
        
    return res

# Negative keywords for a campaign
@router.get("/{campaign_id}/negative-keywords")
async def list_campaign_negative_keywords(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    result = await db.execute(
        select(NegativeKeyword).where(NegativeKeyword.campaign_id == c.id)
    )
    return result.scalars().all()

@router.post("/{campaign_id}/negative-keywords")
async def create_campaign_negative_keyword(
    campaign_id: int,
    payload: NegativeKeywordIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    try:
        cleaned_text = clean_keyword_text(payload.keyword_text)
    except ValueError as e:
        raise HTTPException(422, detail=[{"field": "keyword_text", "message": str(e)}])

    # Duplicate check on (campaign_id, keyword_text, match_type)
    existing = (await db.execute(
        select(NegativeKeyword).where(
            NegativeKeyword.campaign_id == c.id,
            NegativeKeyword.keyword_text == cleaned_text,
            NegativeKeyword.match_type == payload.match_type
        )
    )).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Negative keyword already exists in this campaign")

    # Conflict check: exact negative equal to an enabled positive EXACT keyword in the campaign
    if payload.match_type == "NEGATIVE_EXACT":
        exact_pos = (await db.execute(
            select(Keyword)
            .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
            .where(
                AdGroup.campaign_id == c.id,
                Keyword.state == "ENABLED",
                Keyword.match_type == "EXACT",
                Keyword.keyword_text == cleaned_text
            )
        )).scalars().first()
        if exact_pos:
            raise HTTPException(
                status_code=422,
                detail=[{"field": "keyword_text", "message": f"Negative exact keyword '{cleaned_text}' conflicts with an enabled positive EXACT keyword in this campaign"}]
            )

    nk = NegativeKeyword(
        campaign_id=c.id,
        keyword_text=cleaned_text,
        match_type=payload.match_type
    )
    db.add(nk)
    await db.commit()
    await db.refresh(nk)
    return nk

@router.delete("/{campaign_id}/negative-keywords/{nk_id}")
async def delete_campaign_negative_keyword(
    campaign_id: int,
    nk_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    nk = (await db.execute(
        select(NegativeKeyword).where(
            NegativeKeyword.id == nk_id,
            NegativeKeyword.campaign_id == c.id
        )
    )).scalars().first()
    if not nk:
        raise HTTPException(status_code=404, detail="Negative keyword not found")

    await db.delete(nk)
    await db.commit()
    return {"status": "ok"}

# ----------------- Phase 3 Campaign Detail Endpoints -----------------

@router.get("/{campaign_id}/placements")
async def get_campaign_placements(
    campaign_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == c.ads_account_id))).scalars().first()
    today = today_for(account.marketplace) if account else date.today()
    end = end_date or today
    start = start_date or (end - timedelta(days=6))

    ag_bid = (await db.execute(select(AdGroup.default_bid).where(AdGroup.campaign_id == c.id).limit(1))).scalar() or 15.0
    base_bid = round(float(ag_bid), 2)

    placements_meta = [
        ("TOP_OF_SEARCH", "Top of search (first page)", int(c.placement_top_of_search_pct or c.placement_top_pct or 0)),
        ("REST_OF_SEARCH", "Rest of search", int(c.placement_rest_of_search_pct or c.placement_rest_pct or 0)),
        ("PRODUCT_PAGES", "Product pages", int(c.placement_product_pages_pct or c.placement_product_page_pct or 0)),
    ]

    result = []
    for plc, label, mod_pct in placements_meta:
        m_row = (await db.execute(
            select(
                func.coalesce(func.sum(PlacementMetricDaily.impressions), 0),
                func.coalesce(func.sum(PlacementMetricDaily.clicks), 0),
                func.coalesce(func.sum(PlacementMetricDaily.spend), 0.0),
                func.coalesce(func.sum(PlacementMetricDaily.sales), 0.0),
                func.coalesce(func.sum(PlacementMetricDaily.orders), 0)
            ).where(
                PlacementMetricDaily.campaign_id == c.id,
                PlacementMetricDaily.placement == plc,
                PlacementMetricDaily.date >= start,
                PlacementMetricDaily.date <= end
            )
        )).first()

        imp = int(m_row[0]) if m_row else 0
        clk = int(m_row[1]) if m_row else 0
        spd = round(float(m_row[2]), 2) if m_row else 0.0
        sls = round(float(m_row[3]), 2) if m_row else 0.0
        ords = int(m_row[4]) if m_row else 0

        derived = derive(imp, clk, spd, sls, ords)
        max_eff = compute_max_effective_bid(base_bid, mod_pct, c.bidding_strategy, plc)

        result.append({
            "placement": plc,
            "label": label,
            "modifier_pct": mod_pct,
            "base_bid": base_bid,
            "max_effective_bid": max_eff,
            **derived
        })

    return result

@router.patch("/{campaign_id}/placements")
async def update_campaign_placements(
    campaign_id: int,
    payload: PlacementsUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived campaigns cannot be modified")

    top_val = payload.top_of_search_pct if payload.top_of_search_pct is not None else payload.top_pct
    prod_val = payload.product_pages_pct if payload.product_pages_pct is not None else payload.product_page_pct
    rest_val = payload.rest_of_search_pct if payload.rest_of_search_pct is not None else payload.rest_pct

    if top_val is not None:
        if top_val < 0 or top_val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "top_of_search_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_top_of_search_pct = top_val
        c.placement_top_pct = top_val

    if prod_val is not None:
        if prod_val < 0 or prod_val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "product_pages_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_product_pages_pct = prod_val
        c.placement_product_page_pct = prod_val

    if rest_val is not None:
        if rest_val < 0 or rest_val > 900:
            raise HTTPException(status_code=422, detail=[{"field": "rest_of_search_pct", "message": "Placement modifier must be between 0% and 900%"}])
        c.placement_rest_of_search_pct = rest_val
        c.placement_rest_pct = rest_val

    hist = CampaignHistory(
        campaign_id=c.id,
        event_type="PLACEMENT_UPDATE",
        event_title="Placement Multipliers Updated",
        description=f"Top of search: {c.placement_top_of_search_pct}%, Product pages: {c.placement_product_pages_pct}%, Rest of search: {c.placement_rest_of_search_pct}%",
        new_value=f"Top: {c.placement_top_of_search_pct}%, Prod: {c.placement_product_pages_pct}%, Rest: {c.placement_rest_of_search_pct}%",
        user_id=current_user.id
    )
    db.add(hist)

    await db.commit()
    await db.refresh(c)
    return await get_campaign_placements(campaign_id, db=db, current_user=current_user)

@router.get("/{campaign_id}/targets")
async def get_campaign_targets(
    campaign_id: int,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    kind: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == c.ads_account_id))).scalars().first()
    rules = marketplace_or_in(account.marketplace) if account else MARKETPLACE_RULES["IN"]
    today = today_for(account.marketplace) if account else date.today()
    end = end_date or today
    start = start_date or (end - timedelta(days=6))

    items = []
    want_auto = (kind == "auto") or (kind is None and c.targeting_type == "AUTO")
    want_kw = (kind == "keywords") or (kind is None and c.targeting_type == "MANUAL")
    want_prod = (kind == "products")

    if want_auto:
        t_res = (await db.execute(
            select(Target).where(Target.campaign_id == c.id)
        )).scalars().all()

        ag = (await db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id).limit(1))).scalars().first()
        ag_id = ag.id if ag else None
        def_bid = ag.default_bid if ag else 15.0

        if not t_res:
            auto_keys = ["CLOSE_MATCH", "LOOSE_MATCH", "SUBSTITUTES", "COMPLEMENTS"]
            t_res = []
            for k in auto_keys:
                new_tg = Target(
                    campaign_id=c.id,
                    ad_group_id=ag_id,
                    target_type="AUTO_GROUP",
                    expression_type=k,
                    expression_value=k,
                    kind="AUTO_GROUP",
                    value=k,
                    bid=def_bid,
                    state="ENABLED"
                )
                db.add(new_tg)
                t_res.append(new_tg)
            await db.flush()

        for tg in t_res:
            if tg.target_type != "AUTO_GROUP" and tg.kind != "AUTO_GROUP":
                continue
            m_row = (await db.execute(
                select(
                    func.coalesce(func.sum(MetricDaily.impressions), 0),
                    func.coalesce(func.sum(MetricDaily.clicks), 0),
                    func.coalesce(func.sum(MetricDaily.spend), 0.0),
                    func.coalesce(func.sum(MetricDaily.sales), 0.0),
                    func.coalesce(func.sum(MetricDaily.orders), 0)
                ).where(
                    MetricDaily.entity_type == "target",
                    MetricDaily.entity_id == tg.id,
                    MetricDaily.date >= start,
                    MetricDaily.date <= end
                )
            )).first()
            derived = derive(int(m_row[0]), int(m_row[1]), float(m_row[2]), float(m_row[3]), int(m_row[4]))
            sug = MockAdsProvider.suggest_bid(tg.expression_type or tg.value or "auto_group", tg.bid or def_bid, rules)
            items.append({
                "id": tg.id,
                "campaign_id": c.id,
                "ad_group_id": tg.ad_group_id,
                "target_type": "AUTO_GROUP",
                "expression_type": tg.expression_type or tg.value,
                "expression_value": tg.expression_value or tg.value,
                "state": tg.state,
                "bid": tg.bid,
                "suggested_bid_low": sug["low"],
                "suggested_bid_median": sug["median"],
                "suggested_bid_high": sug["high"],
                **derived
            })

    if want_kw:
        ag_ids = (await db.execute(select(AdGroup.id).where(AdGroup.campaign_id == c.id))).scalars().all()
        if ag_ids:
            kw_res = (await db.execute(select(Keyword).where(Keyword.ad_group_id.in_(ag_ids)))).scalars().all()
            for kw in kw_res:
                m_row = (await db.execute(
                    select(
                        func.coalesce(func.sum(MetricDaily.impressions), 0),
                        func.coalesce(func.sum(MetricDaily.clicks), 0),
                        func.coalesce(func.sum(MetricDaily.spend), 0.0),
                        func.coalesce(func.sum(MetricDaily.sales), 0.0),
                        func.coalesce(func.sum(MetricDaily.orders), 0)
                    ).where(
                        MetricDaily.entity_type == "keyword",
                        MetricDaily.entity_id == kw.id,
                        MetricDaily.date >= start,
                        MetricDaily.date <= end
                    )
                )).first()
                derived = derive(int(m_row[0]), int(m_row[1]), float(m_row[2]), float(m_row[3]), int(m_row[4]))
                sug = MockAdsProvider.suggest_bid(kw.keyword_text, kw.bid or 15.0, rules)
                items.append({
                    "id": kw.id,
                    "campaign_id": c.id,
                    "ad_group_id": kw.ad_group_id,
                    "target_type": "KEYWORD",
                    "keyword_text": kw.keyword_text,
                    "match_type": kw.match_type,
                    "state": kw.state,
                    "bid": kw.bid,
                    "suggested_bid_low": sug["low"],
                    "suggested_bid_median": sug["median"],
                    "suggested_bid_high": sug["high"],
                    **derived
                })

    if want_prod:
        t_res = (await db.execute(
            select(Target).where(
                Target.campaign_id == c.id,
                Target.target_type.in_(["PRODUCT", "CATEGORY", "ASIN", "ASIN_SAME_AS", "CATEGORY_SAME_AS"])
            )
        )).scalars().all()
        for tg in t_res:
            m_row = (await db.execute(
                select(
                    func.coalesce(func.sum(MetricDaily.impressions), 0),
                    func.coalesce(func.sum(MetricDaily.clicks), 0),
                    func.coalesce(func.sum(MetricDaily.spend), 0.0),
                    func.coalesce(func.sum(MetricDaily.sales), 0.0),
                    func.coalesce(func.sum(MetricDaily.orders), 0)
                ).where(
                    MetricDaily.entity_type == "target",
                    MetricDaily.entity_id == tg.id,
                    MetricDaily.date >= start,
                    MetricDaily.date <= end
                )
            )).first()
            derived = derive(int(m_row[0]), int(m_row[1]), float(m_row[2]), float(m_row[3]), int(m_row[4]))
            sug = MockAdsProvider.suggest_bid(tg.expression_value or tg.value or "target", tg.bid or 15.0, rules)
            items.append({
                "id": tg.id,
                "campaign_id": c.id,
                "ad_group_id": tg.ad_group_id,
                "target_type": tg.target_type or tg.kind,
                "expression_type": tg.expression_type,
                "expression_value": tg.expression_value or tg.value,
                "state": tg.state,
                "bid": tg.bid,
                "suggested_bid_low": sug["low"],
                "suggested_bid_median": sug["median"],
                "suggested_bid_high": sug["high"],
                **derived
            })

    return items

@router.patch("/{campaign_id}/targets/{target_id}")
async def patch_campaign_target(
    campaign_id: int,
    target_id: int,
    payload: TargetPatch,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived campaigns cannot be modified")

    if payload.bid is not None:
        if payload.bid < 1.0 or payload.bid > 5000.0:
            raise HTTPException(status_code=422, detail=[{"field": "bid", "message": "Bid must be between ₹1.00 and ₹5,000.00"}])

    tg = (await db.execute(
        select(Target).where(Target.id == target_id, Target.campaign_id == c.id)
    )).scalars().first()

    if tg:
        old_bid = tg.bid
        if payload.bid is not None:
            tg.bid = payload.bid
        if payload.state is not None:
            tg.state = payload.state
        hist = CampaignHistory(
            campaign_id=c.id,
            event_type="TARGET_UPDATE",
            event_title="Target Updated",
            previous_value=f"₹{old_bid:.2f}" if old_bid else None,
            new_value=f"₹{tg.bid:.2f}" if tg.bid else None,
            description=f"Target {tg.expression_type or tg.value} bid set to ₹{tg.bid:.2f}",
            user_id=current_user.id
        )
        db.add(hist)
        await db.commit()
        await db.refresh(tg)
        return {"id": tg.id, "state": tg.state, "bid": tg.bid}

    ag_ids = (await db.execute(select(AdGroup.id).where(AdGroup.campaign_id == c.id))).scalars().all()
    kw = (await db.execute(
        select(Keyword).where(Keyword.id == target_id, Keyword.ad_group_id.in_(ag_ids))
    )).scalars().first()

    if kw:
        old_bid = kw.bid
        if payload.bid is not None:
            kw.bid = payload.bid
        if payload.state is not None:
            kw.state = payload.state
        hist = CampaignHistory(
            campaign_id=c.id,
            event_type="KEYWORD_UPDATE",
            event_title="Keyword Updated",
            previous_value=f"₹{old_bid:.2f}" if old_bid else None,
            new_value=f"₹{kw.bid:.2f}" if kw.bid else None,
            description=f"Keyword '{kw.keyword_text}' bid set to ₹{kw.bid:.2f}",
            user_id=current_user.id
        )
        db.add(hist)
        await db.commit()
        await db.refresh(kw)
        return {"id": kw.id, "state": kw.state, "bid": kw.bid}

    raise HTTPException(status_code=404, detail="Target not found")

@router.get("/{campaign_id}/negatives")
async def get_campaign_negatives(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    nk_res = (await db.execute(
        select(NegativeKeyword).where(NegativeKeyword.campaign_id == c.id)
    )).scalars().all()

    items = []
    for nk in nk_res:
        items.append({
            "id": nk.id,
            "type": "KEYWORD",
            "keyword_text": nk.keyword_text,
            "match_type": nk.match_type,
            "level": "CAMPAIGN"
        })

    for b in (c.negative_brands or []):
        items.append({
            "id": f"brand_{b}",
            "type": "BRAND",
            "keyword_text": b,
            "match_type": "NEGATIVE_BRAND",
            "level": "CAMPAIGN"
        })

    return {
        "negative_keywords": nk_res,
        "negative_brands": c.negative_brands or [],
        "items": items
    }

@router.post("/{campaign_id}/negatives")
async def create_campaign_negative(
    campaign_id: int,
    payload: NegativeIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived campaigns cannot be modified")

    if payload.match_type == "NEGATIVE_BRAND" or payload.brand:
        brand_val = (payload.brand or payload.keyword_text or "").strip()
        if not brand_val:
            raise HTTPException(status_code=422, detail=[{"field": "brand", "message": "Brand cannot be empty"}])
        brands = list(c.negative_brands or [])
        if brand_val.lower() not in [b.lower() for b in brands]:
            brands.append(brand_val)
            c.negative_brands = brands
            hist = CampaignHistory(
                campaign_id=c.id,
                event_type="NEGATE_BRAND",
                event_title="Brand Excluded",
                description=f"Brand '{brand_val}' added to negative brands.",
                new_value=brand_val,
                user_id=current_user.id
            )
            db.add(hist)
            await db.commit()
        return {"status": "ok", "type": "BRAND", "brand": brand_val}

    if not payload.keyword_text:
        raise HTTPException(status_code=422, detail=[{"field": "keyword_text", "message": "Keyword text cannot be empty"}])

    clean_text = clean_keyword_text(payload.keyword_text)
    match_type = payload.match_type or "NEGATIVE_EXACT"

    # Conflict check against active positive EXACT keyword
    if match_type == "NEGATIVE_EXACT":
        pos_kw = (await db.execute(
            select(Keyword)
            .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
            .where(
                AdGroup.campaign_id == c.id,
                Keyword.state == "ENABLED",
                Keyword.match_type == "EXACT",
                Keyword.keyword_text == clean_text
            )
        )).scalars().first()
        if pos_kw:
            raise HTTPException(
                status_code=422,
                detail=[{"field": "keyword_text", "message": f"Negative exact keyword '{clean_text}' conflicts with an active positive exact keyword"}]
            )

    existing = (await db.execute(
        select(NegativeKeyword).where(
            NegativeKeyword.campaign_id == c.id,
            NegativeKeyword.keyword_text == clean_text,
            NegativeKeyword.match_type == match_type
        )
    )).scalars().first()
    if existing:
        raise HTTPException(status_code=409, detail="Negative keyword already exists in this campaign")

    nk = NegativeKeyword(
        campaign_id=c.id,
        keyword_text=clean_text,
        match_type=match_type
    )
    db.add(nk)
    hist = CampaignHistory(
        campaign_id=c.id,
        event_type="NEGATE_KEYWORD",
        event_title="Added Negative Keyword",
        description=f"Negative keyword '{clean_text}' ({match_type}) added.",
        new_value=clean_text,
        user_id=current_user.id
    )
    db.add(hist)
    await db.commit()
    await db.refresh(nk)
    return nk

@router.delete("/{campaign_id}/negatives/{neg_id}")
async def delete_campaign_negative(
    campaign_id: int,
    neg_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived campaigns cannot be modified")

    if str(neg_id).startswith("brand_"):
        b_name = str(neg_id).replace("brand_", "").strip()
        brands = [b for b in (c.negative_brands or []) if b.lower() != b_name.lower()]
        c.negative_brands = brands
        await db.commit()
        return {"status": "ok"}

    try:
        nk_int_id = int(neg_id)
        nk = (await db.execute(
            select(NegativeKeyword).where(NegativeKeyword.id == nk_int_id, NegativeKeyword.campaign_id == c.id)
        )).scalars().first()
        if nk:
            await db.delete(nk)
            await db.commit()
            return {"status": "ok"}
    except ValueError:
        pass

    raise HTTPException(status_code=404, detail="Negative item not found")

@router.post("/{campaign_id}/search-terms/harvest")
async def harvest_search_term(
    campaign_id: int,
    payload: SearchTermHarvestIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived campaigns cannot be modified")

    if payload.bid < 1.0 or payload.bid > 5000.0:
        raise HTTPException(status_code=422, detail=[{"field": "bid", "message": "Bid must be between ₹1.00 and ₹5,000.00"}])

    clean_text = clean_keyword_text(payload.search_term)

    ag_id = payload.ad_group_id
    if not ag_id:
        ag = (await db.execute(select(AdGroup).where(AdGroup.campaign_id == c.id).limit(1))).scalars().first()
        if not ag:
            ag = AdGroup(campaign_id=c.id, name=f"{c.name} Ad Group", default_bid=payload.bid)
            db.add(ag)
            await db.flush()
        ag_id = ag.id
    else:
        ag = (await db.execute(select(AdGroup).where(AdGroup.id == ag_id, AdGroup.campaign_id == c.id))).scalars().first()
        if not ag:
            raise HTTPException(status_code=404, detail="Ad group not found in this campaign")

    kw = (await db.execute(
        select(Keyword).where(
            Keyword.ad_group_id == ag_id,
            Keyword.keyword_text == clean_text,
            Keyword.match_type == payload.match_type
        )
    )).scalars().first()

    if kw:
        kw.bid = payload.bid
        kw.state = "ENABLED"
    else:
        kw = Keyword(
            ad_group_id=ag_id,
            keyword_text=clean_text,
            match_type=payload.match_type,
            bid=payload.bid,
            state="ENABLED"
        )
        db.add(kw)
        await db.flush()

    hist = CampaignHistory(
        campaign_id=c.id,
        event_type="HARVEST_KEYWORD",
        event_title="Harvested Customer Search Term",
        description=f"Search term '{clean_text}' harvested as {payload.match_type} keyword with ₹{payload.bid:.2f} bid.",
        new_value=f"₹{payload.bid:.2f}",
        user_id=current_user.id
    )
    db.add(hist)
    await db.commit()
    await db.refresh(kw)

    return {
        "status": "ok",
        "keyword_id": kw.id,
        "keyword_text": kw.keyword_text,
        "match_type": kw.match_type,
        "bid": kw.bid
    }

@router.post("/{campaign_id}/search-terms/negate")
async def negate_search_term(
    campaign_id: int,
    payload: SearchTermNegateIn,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    if c.state == "ARCHIVED":
        raise HTTPException(status_code=409, detail="Archived campaigns cannot be modified")

    clean_text = clean_keyword_text(payload.search_term)

    if payload.match_type == "NEGATIVE_EXACT":
        pos_kw = (await db.execute(
            select(Keyword)
            .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
            .where(
                AdGroup.campaign_id == c.id,
                Keyword.state == "ENABLED",
                Keyword.match_type == "EXACT",
                Keyword.keyword_text == clean_text
            )
        )).scalars().first()
        if pos_kw:
            raise HTTPException(
                status_code=422,
                detail=[{"field": "keyword_text", "message": f"Negative exact keyword '{clean_text}' conflicts with an active positive exact keyword"}]
            )

    nk = (await db.execute(
        select(NegativeKeyword).where(
            NegativeKeyword.campaign_id == c.id,
            NegativeKeyword.keyword_text == clean_text,
            NegativeKeyword.match_type == payload.match_type
        )
    )).scalars().first()

    if not nk:
        nk = NegativeKeyword(
            campaign_id=c.id,
            keyword_text=clean_text,
            match_type=payload.match_type
        )
        db.add(nk)
        await db.flush()

    hist = CampaignHistory(
        campaign_id=c.id,
        event_type="NEGATE_KEYWORD",
        event_title="Negated Customer Search Term",
        description=f"Search term '{clean_text}' added as {payload.match_type}.",
        new_value=clean_text,
        user_id=current_user.id
    )
    db.add(hist)
    await db.commit()
    await db.refresh(nk)

    return {
        "status": "ok",
        "negative_keyword_id": nk.id,
        "keyword_text": nk.keyword_text,
        "match_type": nk.match_type
    }

@router.get("/{campaign_id}/history")
async def get_campaign_history(
    campaign_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    c = await load_owned_campaign(db, campaign_id, current_user)
    
    h_rows = (await db.execute(
        select(CampaignHistory).where(CampaignHistory.campaign_id == c.id).order_by(CampaignHistory.created_at.desc())
    )).scalars().all()

    rec_rows = (await db.execute(
        select(Recommendation).where(
            Recommendation.campaign_id == c.id,
            Recommendation.status == "APPLIED"
        ).order_by(Recommendation.resolved_at.desc())
    )).scalars().all()

    events = []
    for h in h_rows:
        events.append({
            "id": f"hist_{h.id}",
            "event_type": h.event_type,
            "event_title": h.event_title,
            "description": h.description,
            "previous_value": h.previous_value,
            "new_value": h.new_value,
            "created_at": h.created_at.isoformat() if h.created_at else None,
            "user_id": h.user_id
        })

    for r in rec_rows:
        events.append({
            "id": f"rec_{r.id}",
            "event_type": "RECOMMENDATION_APPLIED",
            "event_title": f"Applied Recommendation: {r.rule_name}",
            "description": f"Applied suggestion on {r.entity_type} {r.entity_label}",
            "previous_value": None,
            "new_value": str(r.apply_result) if r.apply_result else None,
            "created_at": r.resolved_at.isoformat() if r.resolved_at else (r.created_at.isoformat() if r.created_at else None),
            "user_id": r.resolved_by
        })

    events.sort(key=lambda x: x["created_at"] or "", reverse=True)
    return events
