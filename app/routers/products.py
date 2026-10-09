from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, text, literal
from typing import List, Optional
from datetime import date, timedelta

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.models.product import Product
from app.models.product_review import ProductReview
from app.models.campaign import Campaign
from app.models.campaign_product import CampaignProduct
from app.models.metric_daily import MetricDaily
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.negative_keyword import NegativeKeyword
from app.models.ads_account import AdsAccount
from app.core.marketplace_rules import today_for

router = APIRouter(prefix="/products", tags=["products"])

@router.get("")
async def list_products(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    # Fetch all products + aggregate metrics for the account
    # We'll use a single query with outerjoin
    
    # First get the first account to know marketplace today
    account_res = await db.execute(select(AdsAccount).where(AdsAccount.user_id == current_user.id).limit(1))
    account = account_res.scalars().first()
    if not account:
        return []

    # Get aggregated metrics per product via campaigns
    metrics_subq = (
        select(
            CampaignProduct.product_id,
            func.sum(MetricDaily.spend).label("spend"),
            func.sum(MetricDaily.sales).label("sales")
        )
        .join(Campaign, CampaignProduct.campaign_id == Campaign.id)
        .join(MetricDaily, MetricDaily.entity_id == Campaign.id)
        .where(MetricDaily.entity_type == "campaign")
        .group_by(CampaignProduct.product_id)
        .subquery()
    )

    result = await db.execute(
        select(Product, metrics_subq.c.spend, metrics_subq.c.sales)
        .outerjoin(metrics_subq, Product.id == metrics_subq.c.product_id)
        .join(AdsAccount, Product.ads_account_id == AdsAccount.id)
        .where(AdsAccount.user_id == current_user.id)
    )
    
    response = []
    for p, spend, sales in result:
        spend = spend or 0.0
        sales = sales or 0.0
        acos = (spend / sales * 100) if sales > 0 else None
        
        response.append({
            "id": p.id,
            "asin": p.asin,
            "title": p.title,
            "image_url": p.image_url,
            "category": p.category,
            "price": p.price,
            "rating_avg": p.rating_avg,
            "review_count": p.review_count,
            "spend": spend,
            "sales": sales,
            "acos": acos
        })
    
    return response

@router.get("/{product_id}")
async def get_product(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(
        select(Product)
        .join(AdsAccount)
        .where(Product.id == product_id, AdsAccount.user_id == current_user.id)
    )
    p = result.scalars().first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")

    metric_res = await db.execute(
        select(func.sum(MetricDaily.spend), func.sum(MetricDaily.sales))
        .join(Campaign, Campaign.id == MetricDaily.entity_id)
        .join(CampaignProduct, CampaignProduct.campaign_id == Campaign.id)
        .where(CampaignProduct.product_id == p.id, MetricDaily.entity_type == 'campaign')
    )
    spend, sales = metric_res.first()
    spend = spend or 0.0
    sales = sales or 0.0
    acos = (spend / sales * 100) if sales > 0 else None
    roas = (sales / spend) if spend > 0 else None
    
    return {
        "id": p.id,
        "asin": p.asin,
        "title": p.title,
        "image_url": p.image_url,
        "category": p.category,
        "price": p.price,
        "rating_avg": p.rating_avg,
        "review_count": p.review_count,
        "spend": spend,
        "sales": sales,
        "acos": acos,
        "roas": roas
    }

@router.get("/{product_id}/keywords")
async def get_product_keywords(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(Product).join(AdsAccount).where(Product.id == product_id, AdsAccount.user_id == current_user.id))
    p = result.scalars().first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
        
    account = (await db.execute(select(AdsAccount).where(AdsAccount.user_id == current_user.id).limit(1))).scalars().first()
    today = today_for(account.marketplace)
    thirty_days_ago = today - timedelta(days=30)
    
    metrics_subq = (
        select(
            MetricDaily.entity_id,
            func.sum(MetricDaily.spend).label("spend"),
            func.sum(MetricDaily.sales).label("sales")
        )
        .where(MetricDaily.entity_type == "keyword", MetricDaily.date >= thirty_days_ago)
        .group_by(MetricDaily.entity_id)
        .subquery()
    )
    
    kw_result = await db.execute(
        select(Keyword, Campaign.name.label("campaign_name"), AdGroup.name.label("ad_group_name"), metrics_subq.c.spend, metrics_subq.c.sales)
        .join(AdGroup, Keyword.ad_group_id == AdGroup.id)
        .join(Campaign, AdGroup.campaign_id == Campaign.id)
        .join(CampaignProduct, CampaignProduct.campaign_id == Campaign.id)
        .outerjoin(metrics_subq, Keyword.id == metrics_subq.c.entity_id)
        .where(CampaignProduct.product_id == product_id)
    )
    
    response = []
    for kw, c_name, ag_name, spend, sales in kw_result:
        spend = spend or 0.0
        sales = sales or 0.0
        acos = (spend / sales * 100) if sales > 0 else None
        response.append({
            "id": kw.id,
            "keyword_text": kw.keyword_text,
            "match_type": kw.match_type,
            "state": kw.state,
            "bid": kw.bid,
            "campaign_name": c_name,
            "ad_group_name": ag_name,
            "spend": spend,
            "sales": sales,
            "acos": acos
        })
    return response

@router.get("/{product_id}/negative-keywords")
async def get_product_negative_keywords(
    product_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(Product).join(AdsAccount).where(Product.id == product_id, AdsAccount.user_id == current_user.id))
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Product not found")
    
    nk_res = await db.execute(
        select(NegativeKeyword, Campaign.name.label("campaign_name"))
        .join(Campaign, NegativeKeyword.campaign_id == Campaign.id)
        .join(CampaignProduct, CampaignProduct.campaign_id == Campaign.id)
        .where(CampaignProduct.product_id == product_id)
    )
    
    return [
        {
            "id": nk.id,
            "keyword_text": nk.keyword_text,
            "match_type": nk.match_type,
            "campaign_name": c_name
        }
        for nk, c_name in nk_res
    ]

@router.get("/{product_id}/reviews")
async def get_product_reviews(
    product_id: int,
    page: int = 1,
    limit: int = 5,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(Product).join(AdsAccount).where(Product.id == product_id, AdsAccount.user_id == current_user.id))
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Product not found")
        
    offset = (page - 1) * limit
    rev_result = await db.execute(
        select(ProductReview)
        .where(ProductReview.product_id == product_id)
        .order_by(ProductReview.review_date.desc())
        .offset(offset)
        .limit(limit)
    )
    reviews = rev_result.scalars().all()
    
    if page == 1:
        dist_res = await db.execute(
            select(ProductReview.rating, func.count(ProductReview.id))
            .where(ProductReview.product_id == product_id)
            .group_by(ProductReview.rating)
        )
        distribution = {row[0]: row[1] for row in dist_res}
        for i in range(1, 6):
            if i not in distribution:
                distribution[i] = 0
                
        trend_res = await db.execute(
            select(ProductReview.review_date, func.avg(ProductReview.rating))
            .where(ProductReview.product_id == product_id)
            .group_by(ProductReview.review_date)
            .order_by(ProductReview.review_date)
        )
        trend = [{"date": row[0].isoformat(), "rating": float(row[1])} for row in trend_res]
        
        return {
            "reviews": reviews,
            "distribution": distribution,
            "trend": trend
        }
    
    return {"reviews": reviews}
