from datetime import date, timedelta
from typing import Literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_
from sqlalchemy.orm import selectinload
from app.models.campaign import Campaign
from app.models.metric_daily import MetricDaily
from app.models.user_campaign_settings import UserCampaignSettings
from app.models.ads_account import AdsAccount

def derive(m: dict) -> dict:
    imp = float(m.get("impressions") or 0)
    clk = float(m.get("clicks") or 0)
    spend = float(m.get("spend") or 0)
    sales = float(m.get("sales") or 0)
    orders = float(m.get("orders") or 0)
    organic = float(m.get("organic_sales") or 0)
    return {**m,
        "ctr":  clk / imp * 100 if imp else None,
        "cpc":  spend / clk if clk else None,
        "cvr":  orders / clk * 100 if clk else None,
        "acos": spend / sales * 100 if sales else None,          
        "roas": sales / spend if spend else None,
        "tacos": spend / (sales + organic) * 100 if (sales + organic) else None}

def determine_health(c: Campaign, m: dict, today: date, settings: UserCampaignSettings):
    imp, clk, spend, sales, orders = (m.get(k) or 0 for k in ("impressions","clicks","spend","sales","orders"))
    acos = m.get("acos")
    
    age = (today - c.start_date).days
    if age < 7 or clk < 20:
        return "LEARNING"
        
    acos_high = settings.acos_high if settings else 90.0
    if spend > 0 and (sales == 0 or (acos is not None and acos > acos_high)):
        return "BUDGET_LEAK"
        
    target = c.target_acos or (settings.default_target_acos if settings else 30.0)
    if acos is not None and acos <= target and orders >= 3:
        return "SCALE"
        
    return "HEALTHY"

async def get_report(
    db: AsyncSession, 
    account: AdsAccount,
    start: date, end: date,
    q: str = None, state: list[str] = None, targeting_type: str = None,
    min_budget: float = None, max_budget: float = None,
    min_acos: float = None, max_acos: float = None,
    sort: str = "spend", dir: str = "desc",
    page: int = 1, page_size: int = 50, export: bool = False
):
    from sqlalchemy.sql import nullslast
    settings = (await db.execute(select(UserCampaignSettings).where(UserCampaignSettings.user_id == account.user_id))).scalars().first()
    if not settings:
        settings = UserCampaignSettings(user_id=account.user_id, acos_low=20.0, acos_high=90.0, default_target_acos=30.0)
        db.add(settings)
        await db.commit()
    
    # Subquery for grouped metrics
    metric_sq = (
        select(
            MetricDaily.entity_id.label("campaign_id"),
            func.sum(MetricDaily.impressions).label("impressions"),
            func.sum(MetricDaily.clicks).label("clicks"),
            func.sum(MetricDaily.spend).label("spend"),
            func.sum(MetricDaily.sales).label("sales"),
            func.sum(MetricDaily.orders).label("orders"),
            func.sum(MetricDaily.organic_sales).label("organic_sales")
        )
        .where(MetricDaily.entity_type == "campaign", MetricDaily.date >= start, MetricDaily.date <= end)
        .group_by(MetricDaily.entity_id)
        .subquery()
    )

    query = (
        select(Campaign, metric_sq)
        .outerjoin(metric_sq, Campaign.id == metric_sq.c.campaign_id)
        .where(Campaign.ads_account_id == account.id)
    )
    
    if q:
        query = query.where(func.lower(Campaign.name).like(f"%{q.lower()}%"))
    if state:
        query = query.where(Campaign.state.in_(state))
    if targeting_type:
        query = query.where(Campaign.targeting_type == targeting_type)
    if min_budget is not None:
        query = query.where(Campaign.daily_budget >= min_budget)
    if max_budget is not None:
        query = query.where(Campaign.daily_budget <= max_budget)
        
    # We load products
    query = query.options(selectinload(Campaign.product))
    
    # Fetch all, filter health/acos in Python to avoid complex SQL, unless we need SQL sort on ACOS
    # For ACOS sort in SQL:
    from sqlalchemy import Float
    acos_expr = func.cast(metric_sq.c.spend, Float()) / func.cast(func.nullif(metric_sq.c.sales, 0), Float()) * 100
    if min_acos is not None:
        query = query.where(acos_expr >= min_acos)
    if max_acos is not None:
        query = query.where(acos_expr <= max_acos)
        
    # Sorting
    sort_col = getattr(metric_sq.c, sort, None)
    if sort_col is None and hasattr(Campaign, sort):
        sort_col = getattr(Campaign, sort)
    
    if sort == "acos": sort_col = acos_expr
    elif sort == "roas": sort_col = func.cast(metric_sq.c.sales, Float()) / func.cast(func.nullif(metric_sq.c.spend, 0), Float())
    elif sort == "ctr": sort_col = func.cast(metric_sq.c.clicks, Float()) / func.cast(func.nullif(metric_sq.c.impressions, 0), Float())
    elif sort == "cpc": sort_col = func.cast(metric_sq.c.spend, Float()) / func.cast(func.nullif(metric_sq.c.clicks, 0), Float())
    elif sort == "cvr": sort_col = func.cast(metric_sq.c.orders, Float()) / func.cast(func.nullif(metric_sq.c.clicks, 0), Float())
        
    if sort_col is not None:
        query = query.order_by(nullslast(sort_col.desc() if dir == "desc" else sort_col.asc()))

    # Execute all filtered rows to get totals and do pagination in Python if needed, 
    # but the instruction says "GET /campaigns/report totals equal the sum over all filtered rows"
    # To do pagination in SQL we need a separate totals query.
    
    # Count query
    from sqlalchemy import select as sql_select, func as sql_func
    sq = query.subquery()
    count_query = sql_select(sql_func.count()).select_from(sq)
    total_count = (await db.execute(count_query)).scalar()
    
    # Totals query
    totals_q = sql_select(
        sql_func.sum(sq.c.impressions),
        sql_func.sum(sq.c.clicks),
        sql_func.sum(sq.c.spend),
        sql_func.sum(sq.c.sales),
        sql_func.sum(sq.c.orders),
        sql_func.sum(sq.c.organic_sales)
    ).select_from(sq)
    t_res = (await db.execute(totals_q)).first()
    t_dict = {
        "impressions": t_res[0] or 0, "clicks": t_res[1] or 0, "spend": t_res[2] or 0.0,
        "sales": t_res[3] or 0.0, "orders": t_res[4] or 0, "organic_sales": t_res[5] or 0.0
    }
    totals = derive(t_dict)
    
    # Paging
    if not export:
        query = query.limit(page_size).offset((page - 1) * page_size)
    else:
        query = query.limit(10000)
        
    rows = (await db.execute(query)).all()
    from app.core.marketplace_rules import today_for
    today = today_for(account.marketplace)
    
    items = []
    for row in rows:
        c = row[0]
        m_dict = {
            "impressions": row[2] or 0, "clicks": row[3] or 0, "spend": row[4] or 0.0,
            "sales": row[5] or 0.0, "orders": row[6] or 0, "organic_sales": row[7] or 0.0
        }
        derived = derive(m_dict)
        
        start_bound = max(c.start_date, start)
        end_bound = min(c.end_date or end, end)
        days_in_range_after_start = max(1, (end_bound - start_bound).days + 1)
        budget_utilization = derived["spend"] / (c.daily_budget * days_in_range_after_start) if c.daily_budget else 0.0
        
        status_label = c.state
        if c.state == "ENABLED":
            if c.start_date > today: status_label = "SCHEDULED"
            elif c.end_date and c.end_date < today: status_label = "ENDED"
            
        products_list = []
        if c.product:
            products_list.append({
                "id": c.product.id, "asin": c.product.asin, "title": c.product.title, "image_url": c.product.image_url
            })
            
        items.append({
            "id": c.id, "name": c.name, "state": c.state, "status_label": status_label,
            "start_date": c.start_date, "end_date": c.end_date, "daily_budget": c.daily_budget,
            "targeting_type": c.targeting_type, "bidding_strategy": c.bidding_strategy,
            "target_acos": c.target_acos, "products": products_list,
            **derived,
            "budget_utilization": budget_utilization,
            "limited_by_budget": budget_utilization >= 0.9,
            "health": determine_health(c, derived, today, settings)
        })
        
    # We might need to filter by health post-SQL since it's computed in Python
    # But filtering by health in Python ruins pagination unless we fetch all.
    # The requirement didn't specify exactly, but to be robust let's just return what we have.
    
    from app.core.marketplace_rules import MARKETPLACE_RULES
    rules = MARKETPLACE_RULES.get(account.marketplace, MARKETPLACE_RULES["US"])
    
    return {
        "items": items,
        "total_count": total_count,
        "page": page,
        "page_size": page_size,
        "totals": totals,
        "currency": rules["currency"],
        "symbol": rules["symbol"],
        "locale": rules["locale"]
    }

async def get_summary(
    db: AsyncSession, account: AdsAccount,
    start: date, end: date, granularity: str = "day"
):
    query = (
        select(MetricDaily)
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= start,
            MetricDaily.date <= end
        )
    )
    rows = (await db.execute(query)).scalars().all()
    
    buckets = {}
    
    def get_bucket_date(d: date):
        if granularity == "week":
            return d - timedelta(days=d.weekday())
        elif granularity == "month":
            return d.replace(day=1)
        return d
        
    for r in rows:
        bd = get_bucket_date(r.date)
        if bd not in buckets:
            buckets[bd] = {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0}
        buckets[bd]["impressions"] += r.impressions
        buckets[bd]["clicks"] += r.clicks
        buckets[bd]["spend"] += r.spend
        buckets[bd]["sales"] += r.sales
        buckets[bd]["orders"] += (r.orders or 0)
        buckets[bd]["organic_sales"] += (r.organic_sales or 0.0)
        
    series = []
    # zero-fill
    current = get_bucket_date(start)
    end_bucket = get_bucket_date(end)
    while current <= end_bucket:
        b = buckets.get(current, {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0})
        series.append({
            "date": current,
            **derive(b)
        })
        if granularity == "month":
            if current.month == 12: current = current.replace(year=current.year+1, month=1)
            else: current = current.replace(month=current.month+1)
        elif granularity == "week": current += timedelta(days=7)
        else: current += timedelta(days=1)
        
    # totals
    total_dict = {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0}
    for b in buckets.values():
        for k in total_dict: total_dict[k] += b[k]
        
    # previous period
    days_diff = (end - start).days + 1
    prev_start = start - timedelta(days=days_diff)
    prev_end = start - timedelta(days=1)
    
    prev_query = (
        select(MetricDaily)
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .where(
            Campaign.ads_account_id == account.id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= prev_start,
            MetricDaily.date <= prev_end
        )
    )
    prev_rows = (await db.execute(prev_query)).scalars().all()
    prev_dict = {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0}
    for r in prev_rows:
        prev_dict["impressions"] += r.impressions
        prev_dict["clicks"] += r.clicks
        prev_dict["spend"] += r.spend
        prev_dict["sales"] += r.sales
        prev_dict["orders"] += (r.orders or 0)
        prev_dict["organic_sales"] += (r.organic_sales or 0.0)
        
    t_derive = derive(total_dict)
    p_derive = derive(prev_dict)
    
    delta_pct = {}
    for k in t_derive:
        cv = t_derive[k]
        pv = p_derive[k]
        if cv is None or pv is None or pv == 0:
            delta_pct[k] = None
        else:
            delta_pct[k] = (cv - pv) / abs(pv) * 100
            
    return {
        "series": series,
        "totals": t_derive,
        "previous_totals": p_derive,
        "delta_pct": delta_pct
    }

async def account_totals(db: AsyncSession, account_id: int, days: int = 30):
    account = (await db.execute(select(AdsAccount).where(AdsAccount.id == account_id))).scalars().first()
    from app.core.marketplace_rules import today_for
    today = today_for(account.marketplace)
    start = today - timedelta(days=days)
    
    query = (
        select(
            func.sum(MetricDaily.impressions),
            func.sum(MetricDaily.clicks),
            func.sum(MetricDaily.spend),
            func.sum(MetricDaily.sales),
            func.sum(MetricDaily.orders),
            func.sum(MetricDaily.organic_sales)
        )
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .where(
            Campaign.ads_account_id == account_id,
            MetricDaily.entity_type == "campaign",
            MetricDaily.date >= start,
            MetricDaily.date <= today
        )
    )
    row = (await db.execute(query)).first()
    d = {
        "impressions": row[0] or 0, "clicks": row[1] or 0, "spend": row[2] or 0.0,
        "sales": row[3] or 0.0, "orders": row[4] or 0, "organic_sales": row[5] or 0.0
    }
    return derive(d)
