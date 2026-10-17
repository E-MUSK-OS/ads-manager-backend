from datetime import date, timedelta
from typing import Literal, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, or_, Float
from sqlalchemy.orm import selectinload
from sqlalchemy.sql import nullslast
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
    return {
        **m,
        "ctr": clk / imp * 100 if imp else None,
        "cpc": spend / clk if clk else None,
        "cvr": orders / clk * 100 if clk else None,
        "acos": spend / sales * 100 if sales else None,
        "roas": sales / spend if spend else None,
        "tacos": spend / (sales + organic) * 100 if (sales + organic) else None,
    }

def get_default_health_rules(acos_high: float = 90.0) -> list[dict]:
    return [
        {
            "key": "LEARNING",
            "name": "Learning",
            "color": "blue",
            "enabled": True,
            "match": "ANY",
            "conditions": [
                {"metric": "age_days", "op": "<", "value": 7},
                {"metric": "clicks", "op": "<", "value": 20}
            ]
        },
        {
            "key": "BUDGET_LEAK",
            "name": "Budget Leak",
            "color": "red",
            "enabled": True,
            "match": "ANY",
            "conditions": [
                {"metric": "acos", "op": ">", "value": acos_high},
                {"metric": "spend_without_sales", "op": ">", "value": 0}
            ]
        },
        {
            "key": "SCALE",
            "name": "Scale",
            "color": "green",
            "enabled": True,
            "match": "ALL",
            "conditions": [
                {"metric": "acos", "op": "<=", "value": "@target_acos"},
                {"metric": "orders", "op": ">=", "value": 3}
            ]
        }
    ]

def evaluate_condition(cond: dict, metrics: dict, target_acos: float) -> bool:
    metric_name = cond.get("metric")
    val = metrics.get(metric_name)
    if val is None:
        return False

    target_val = cond.get("value")
    if target_val == "@target_acos":
        target_val = target_acos
    else:
        try:
            target_val = float(target_val)
        except (ValueError, TypeError):
            return False

    op = cond.get("op")
    if op == ">": return val > target_val
    if op == ">=": return val >= target_val
    if op == "<": return val < target_val
    if op == "<=": return val <= target_val
    if op == "==": return val == target_val
    return False

def determine_health(
    c: Campaign,
    m: dict,
    today: date,
    settings: Optional[UserCampaignSettings]
) -> Tuple[str, str, str]:
    """
    Returns (health_key, health_label, health_color)
    Evaluates rules top to bottom, first enabled match wins, fallback HEALTHY (green).
    """
    age_days = (today - c.start_date).days if c.start_date else 0
    spend = float(m.get("spend") or 0)
    sales = float(m.get("sales") or 0)
    spend_without_sales = spend if sales == 0 else 0.0
    budget = float(c.daily_budget or 0)
    budget_utilization = m.get("budget_utilization")

    derived = {
        "acos": m.get("acos"),
        "roas": m.get("roas"),
        "spend": spend,
        "sales": sales,
        "orders": float(m.get("orders") or 0),
        "clicks": float(m.get("clicks") or 0),
        "impressions": float(m.get("impressions") or 0),
        "ctr": m.get("ctr"),
        "cpc": m.get("cpc"),
        "cvr": m.get("cvr"),
        "budget_utilization": budget_utilization,
        "age_days": age_days,
        "spend_without_sales": spend_without_sales,
    }

    rules = (settings.health_rules if settings and settings.health_rules
             else get_default_health_rules(settings.acos_high if settings else 90.0))
    target_acos = c.target_acos or (settings.default_target_acos if settings else 30.0)

    for r in rules:
        if not r.get("enabled", True):
            continue
        conditions = r.get("conditions", [])
        if not conditions:
            continue
        match_type = r.get("match", "ALL")
        if match_type == "ALL":
            matched = all(evaluate_condition(cond, derived, target_acos) for cond in conditions)
        else:
            matched = any(evaluate_condition(cond, derived, target_acos) for cond in conditions)

        if matched:
            return r.get("key", "HEALTHY"), r.get("name", "Healthy"), r.get("color", "green")

    return "HEALTHY", "Healthy", "green"


from sqlalchemy.exc import IntegrityError

async def get_or_create_user_campaign_settings(db: AsyncSession, user_id: int) -> UserCampaignSettings:
    settings = (await db.execute(
        select(UserCampaignSettings).where(UserCampaignSettings.user_id == user_id)
    )).scalars().first()
    if settings:
        return settings
    
    try:
        settings = UserCampaignSettings(
            user_id=user_id,
            acos_low=20.0,
            acos_high=90.0,
            default_target_acos=30.0
        )
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
        return settings
    except IntegrityError:
        await db.rollback()
        settings = (await db.execute(
            select(UserCampaignSettings).where(UserCampaignSettings.user_id == user_id)
        )).scalars().first()
        return settings

async def get_report(
    db: AsyncSession,
    account: AdsAccount,
    start: date,
    end: date,
    q: Optional[str] = None,
    state: Optional[list[str]] = None,
    targeting_type: Optional[str] = None,
    health: Optional[str] = None,
    min_budget: Optional[float] = None,
    max_budget: Optional[float] = None,
    min_acos: Optional[float] = None,
    max_acos: Optional[float] = None,
    metric_filters: Optional[dict[str, float]] = None,
    sort: str = "spend",
    dir: str = "desc",
    page: int = 1,
    page_size: int = 50,
    export: bool = False
):
    settings = await get_or_create_user_campaign_settings(db, account.user_id)

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

    if q and q.strip():
        query = query.where(func.lower(Campaign.name).like(f"%{q.strip().lower()}%"))
    if state:
        query = query.where(Campaign.state.in_(state))
    if targeting_type:
        query = query.where(Campaign.targeting_type == targeting_type)
    if min_budget is not None:
        query = query.where(Campaign.daily_budget >= min_budget)
    if max_budget is not None:
        query = query.where(Campaign.daily_budget <= max_budget)

    query = query.options(selectinload(Campaign.product))

    acos_expr = func.cast(metric_sq.c.spend, Float()) / func.cast(func.nullif(metric_sq.c.sales, 0), Float()) * 100
    if min_acos is not None:
        query = query.where(acos_expr >= min_acos)
    if max_acos is not None:
        query = query.where(acos_expr <= max_acos)

    # Allow-list dict for generic metric filters (no string-built SQL)
    ALLOWLIST_EXPRS = {
        "spend": metric_sq.c.spend,
        "sales": metric_sq.c.sales,
        "orders": metric_sq.c.orders,
        "clicks": metric_sq.c.clicks,
        "impressions": metric_sq.c.impressions,
        "roas": func.cast(metric_sq.c.sales, Float()) / func.cast(func.nullif(metric_sq.c.spend, 0), Float()),
        "cpc": func.cast(metric_sq.c.spend, Float()) / func.cast(func.nullif(metric_sq.c.clicks, 0), Float()),
        "ctr": func.cast(metric_sq.c.clicks, Float()) / func.cast(func.nullif(metric_sq.c.impressions, 0), Float()) * 100,
    }

    if metric_filters:
        for m_name, expr in ALLOWLIST_EXPRS.items():
            min_v = metric_filters.get(f"min_{m_name}")
            if min_v is not None:
                query = query.where(expr >= min_v)
            max_v = metric_filters.get(f"max_{m_name}")
            if max_v is not None:
                query = query.where(expr <= max_v)

    # Sorting expression
    sort_col = getattr(metric_sq.c, sort, None)
    if sort_col is None and hasattr(Campaign, sort):
        sort_col = getattr(Campaign, sort)

    if sort == "acos": sort_col = acos_expr
    elif sort == "roas": sort_col = ALLOWLIST_EXPRS["roas"]
    elif sort == "ctr": sort_col = ALLOWLIST_EXPRS["ctr"]
    elif sort == "cpc": sort_col = ALLOWLIST_EXPRS["cpc"]
    elif sort == "cvr": sort_col = func.cast(metric_sq.c.orders, Float()) / func.cast(func.nullif(metric_sq.c.clicks, 0), Float())

    if sort_col is not None:
        query = query.order_by(nullslast(sort_col.desc() if dir == "desc" else sort_col.asc()))

    from app.core.marketplace_rules import today_for
    today = today_for(account.marketplace)

    min_util = metric_filters.get("min_utilization") if metric_filters else None
    max_util = metric_filters.get("max_utilization") if metric_filters else None

    # Pre-pagination filtering when health or utilization is requested
    if health or min_util is not None or max_util is not None:
        # Fetch up to 10,000 rows matching SQL filters
        all_rows = (await db.execute(query.limit(10000))).all()
        processed_items = []
        for row in all_rows:
            c = row[0]
            m_dict = {
                "impressions": row[2] or 0, "clicks": row[3] or 0, "spend": row[4] or 0.0,
                "sales": row[5] or 0.0, "orders": row[6] or 0, "organic_sales": row[7] or 0.0
            }
            derived = derive(m_dict)

            start_bound = max(c.start_date, start)
            end_bound = min(c.end_date or end, end)
            days_in_range = max(1, (end_bound - start_bound).days + 1)
            budget_util = derived["spend"] / (c.daily_budget * days_in_range) if c.daily_budget else 0.0
            derived["budget_utilization"] = budget_util

            h_key, h_label, h_color = determine_health(c, derived, today, settings)

            status_label = c.state
            if c.state == "ENABLED":
                if c.start_date > today: status_label = "SCHEDULED"
                elif c.end_date and c.end_date < today: status_label = "ENDED"

            products_list = []
            if c.product:
                products_list.append({
                    "id": c.product.id, "asin": c.product.asin, "sku": c.product.sku,
                    "title": c.product.title, "image_url": c.product.image_url
                })

            item = {
                "id": c.id, "ads_account_id": c.ads_account_id, "name": c.name,
                "state": c.state, "status_label": status_label,
                "site": c.site,
                "start_date": c.start_date.isoformat() if c.start_date else None,
                "end_date": c.end_date.isoformat() if c.end_date else None,
                "daily_budget": c.daily_budget,
                "targeting_type": c.targeting_type, "bidding_strategy": c.bidding_strategy,
                "target_acos": c.target_acos,
                "placement_top_pct": c.placement_top_pct,
                "placement_rest_pct": c.placement_rest_pct,
                "placement_product_page_pct": c.placement_product_page_pct,
                "products": products_list,
                **derived,
                "limited_by_budget": budget_util >= 0.9,
                "health": h_key,
                "health_label": h_label,
                "health_color": h_color
            }
            processed_items.append(item)

        # Apply health filter
        if health:
            processed_items = [it for it in processed_items if it["health"] == health]

        # Apply utilization filters
        if min_util is not None:
            processed_items = [it for it in processed_items if it["budget_utilization"] is not None and it["budget_utilization"] >= min_util]
        if max_util is not None:
            processed_items = [it for it in processed_items if it["budget_utilization"] is not None and it["budget_utilization"] <= max_util]

        # Calculate totals over filtered items
        f_tot = {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0}
        for it in processed_items:
            f_tot["impressions"] += it.get("impressions", 0)
            f_tot["clicks"] += it.get("clicks", 0)
            f_tot["spend"] += it.get("spend", 0.0)
            f_tot["sales"] += it.get("sales", 0.0)
            f_tot["orders"] += it.get("orders", 0)
            f_tot["organic_sales"] += it.get("organic_sales", 0.0)

        totals = derive(f_tot)
        total_count = len(processed_items)

        # In-memory pagination
        if not export:
            offset = max(0, (page - 1) * page_size)
            page_items = processed_items[offset : offset + page_size]
        else:
            page_items = processed_items

        return {
            "items": page_items,
            "totals": totals,
            "total_count": total_count,
            "page": page,
            "page_size": page_size
        }

    # Standard SQL execution (no Python-level health filter)
    sq = query.subquery()
    count_query = select(func.count()).select_from(sq)
    total_count = (await db.execute(count_query)).scalar() or 0

    totals_q = select(
        func.sum(sq.c.impressions),
        func.sum(sq.c.clicks),
        func.sum(sq.c.spend),
        func.sum(sq.c.sales),
        func.sum(sq.c.orders),
        func.sum(sq.c.organic_sales)
    ).select_from(sq)
    t_res = (await db.execute(totals_q)).first()
    t_dict = {
        "impressions": t_res[0] or 0, "clicks": t_res[1] or 0, "spend": t_res[2] or 0.0,
        "sales": t_res[3] or 0.0, "orders": t_res[4] or 0, "organic_sales": t_res[5] or 0.0
    }
    totals = derive(t_dict)

    if not export:
        query = query.limit(page_size).offset(max(0, (page - 1) * page_size))
    else:
        query = query.limit(10000)

    rows = (await db.execute(query)).all()
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
        derived["budget_utilization"] = budget_utilization

        status_label = c.state
        if c.state == "ENABLED":
            if c.start_date > today: status_label = "SCHEDULED"
            elif c.end_date and c.end_date < today: status_label = "ENDED"

        products_list = []
        if c.product:
            products_list.append({
                "id": c.product.id, "asin": c.product.asin, "sku": c.product.sku,
                "title": c.product.title, "image_url": c.product.image_url
            })

        h_key, h_label, h_color = determine_health(c, derived, today, settings)

        items.append({
            "id": c.id, "ads_account_id": c.ads_account_id, "name": c.name,
            "state": c.state, "status_label": status_label,
            "site": c.site,
            "start_date": c.start_date.isoformat() if c.start_date else None,
            "end_date": c.end_date.isoformat() if c.end_date else None,
            "daily_budget": c.daily_budget,
            "targeting_type": c.targeting_type, "bidding_strategy": c.bidding_strategy,
            "target_acos": c.target_acos,
            "placement_top_pct": c.placement_top_pct,
            "placement_rest_pct": c.placement_rest_pct,
            "placement_product_page_pct": c.placement_product_page_pct,
            "products": products_list,
            **derived,
            "limited_by_budget": budget_utilization >= 0.9,
            "health": h_key,
            "health_label": h_label,
            "health_color": h_color
        })

    return {
        "items": items,
        "totals": totals,
        "total_count": total_count,
        "page": page,
        "page_size": page_size
    }


async def get_summary(
    db: AsyncSession,
    account: AdsAccount,
    start: date,
    end: date,
    granularity: str = "day",
    compare: bool = False
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
    current = get_bucket_date(start)
    end_bucket = get_bucket_date(end)
    while current <= end_bucket:
        b = buckets.get(current, {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0})
        series.append({
            "date": current.isoformat(),
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

    response = {
        "series": series,
        "totals": t_derive,
        "previous_totals": p_derive,
        "delta_pct": delta_pct
    }

    if compare:
        prev_buckets = {}
        for r in prev_rows:
            bd = get_bucket_date(r.date)
            if bd not in prev_buckets:
                prev_buckets[bd] = {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0}
            prev_buckets[bd]["impressions"] += r.impressions
            prev_buckets[bd]["clicks"] += r.clicks
            prev_buckets[bd]["spend"] += r.spend
            prev_buckets[bd]["sales"] += r.sales
            prev_buckets[bd]["orders"] += (r.orders or 0)
            prev_buckets[bd]["organic_sales"] += (r.organic_sales or 0.0)

        prev_current = get_bucket_date(prev_start)
        prev_end_bucket = get_bucket_date(prev_end)
        raw_prev = []
        while prev_current <= prev_end_bucket:
            pb = prev_buckets.get(prev_current, {"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0})
            raw_prev.append({
                "date": prev_current.isoformat(),
                **derive(pb)
            })
            if granularity == "month":
                if prev_current.month == 12: prev_current = prev_current.replace(year=prev_current.year+1, month=1)
                else: prev_current = prev_current.replace(month=prev_current.month+1)
            elif granularity == "week": prev_current += timedelta(days=7)
            else: prev_current += timedelta(days=1)

        target_len = len(series)
        if len(raw_prev) < target_len:
            while len(raw_prev) < target_len:
                raw_prev.append({
                    "date": None,
                    **derive({"impressions":0, "clicks":0, "spend":0.0, "sales":0.0, "orders":0, "organic_sales":0.0})
                })
        elif len(raw_prev) > target_len:
            raw_prev = raw_prev[:target_len]

        response["previous_series"] = raw_prev

    return response
