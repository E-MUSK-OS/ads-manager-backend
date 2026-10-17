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
from app.schemas.campaign import (
    CampaignResponse, CampaignCreate, CampaignUpdate, StateUpdate, BulkActionRequest,
    BidSuggestionsRequest, ForecastRequest, NegativeKeywordIn,
    UserCampaignSettingsResponse, UserCampaignSettingsUpdate, clean_keyword_text
)
from app.services.campaign_service import create_campaign, validate_create, load_detail
from app.services.metrics_service import (
    get_report, get_summary, determine_health, derive, get_default_health_rules,
    get_or_create_user_campaign_settings
)
from app.core.marketplace_rules import MARKETPLACE_RULES, today_for, marketplace_or_us
from app.providers.ads.mock_provider import MockAdsProvider

router = APIRouter(prefix="/campaigns", tags=["campaigns"])

@router.get("", response_model=list[CampaignResponse])
async def get_campaigns(db: AsyncSession = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = select(Campaign).join(AdsAccount).where(AdsAccount.user_id == current_user.id)
    result = await db.execute(query)
    return result.scalars().all()

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
    rules = marketplace_or_us(account.marketplace)

    # Return null ranges when products, targets or bid are missing
    if not payload.product_ids or payload.daily_budget is None or payload.default_bid is None or payload.targeting_type is None or (payload.targeting_type == "MANUAL" and not payload.target_count):
        return {
            "period": "weekly",
            "clicks": None,
            "orders": None,
            "spend": None,
            "basis": "default",
            "is_estimate": True
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

    default_bid = payload.default_bid
    daily_budget = payload.daily_budget
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

    return {
        "period": "weekly",
        "clicks": {"low": clicks_low, "high": clicks_high},
        "orders": {"low": orders_low, "high": orders_high},
        "spend": {"low": spend_low, "high": spend_high},
        "basis": "history" if has_history else "default",
        "is_estimate": True
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
    payload: BulkActionRequest,
    db: AsyncSession = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    results = []
    for cid in payload.ids:
        try:
            c = await load_owned_campaign(db, cid, current_user)
            if payload.action == "ENABLE":
                if c.state == "ARCHIVED": raise ValueError("Cannot enable archived campaign")
                c.state = "ENABLED"
            elif payload.action == "PAUSE":
                if c.state == "ARCHIVED": raise ValueError("Cannot pause archived campaign")
                c.state = "PAUSED"
            elif payload.action == "ARCHIVE":
                c.state = "ARCHIVED"
            elif payload.action == "SET_BUDGET":
                if payload.value is None or payload.value < 0: raise ValueError("Invalid budget")
                c.daily_budget = payload.value
            elif payload.action == "CHANGE_BUDGET_PCT":
                if payload.value is None: raise ValueError("Invalid pct")
                c.daily_budget = c.daily_budget * (1 + payload.value / 100.0)
            
            await db.commit()
            results.append({"id": cid, "ok": True})
        except Exception as e:
            await db.rollback()
            results.append({"id": cid, "ok": False, "error": str(e)})
            
    return {"results": results}

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
        "targeting_type": c.targeting_type,
        "campaign_type": c.campaign_type,
        "site": c.site,
        "start_date": c.start_date,
        "end_date": c.end_date,
        "daily_budget": c.daily_budget,
        "target_acos": c.target_acos,
        "bidding_strategy": c.bidding_strategy,
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
    rules = marketplace_or_us(account.marketplace)
    
    if payload.name is not None:
        c.name = payload.name.strip()
    if payload.daily_budget is not None:
        if payload.daily_budget < rules["min_budget"]:
            raise HTTPException(422, detail=[{"field": "daily_budget", "message": f"Minimum daily budget is {rules['symbol']}{rules['min_budget']}"}])
        c.daily_budget = payload.daily_budget
    if getattr(payload, 'end_date', object()) is not None:
        c.end_date = payload.end_date
    if payload.target_acos is not None:
        c.target_acos = payload.target_acos
    if payload.bidding_strategy is not None:
        c.bidding_strategy = payload.bidding_strategy
    if payload.placement_top_pct is not None:
        c.placement_top_pct = payload.placement_top_pct
    if payload.placement_rest_pct is not None:
        c.placement_rest_pct = payload.placement_rest_pct
    if payload.placement_product_page_pct is not None:
        c.placement_product_page_pct = payload.placement_product_page_pct
        
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
    today = today_for(account.marketplace)
    
    new_c = Campaign(
        ads_account_id=c.ads_account_id,
        name=f"{c.name} (Copy)",
        state="PAUSED",
        targeting_type=c.targeting_type,
        campaign_type=c.campaign_type,
        site=c.site,
        start_date=today,
        end_date=None,
        daily_budget=c.daily_budget,
        target_acos=c.target_acos,
        bidding_strategy=c.bidding_strategy,
        placement_top_pct=c.placement_top_pct,
        placement_rest_pct=c.placement_rest_pct,
        placement_product_page_pct=c.placement_product_page_pct,
        product_id=c.product_id
    )
    db.add(new_c)
    await db.flush()
    
    await db.commit()
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
