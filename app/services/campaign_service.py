import re
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, exc
from sqlalchemy.orm import selectinload
from app.models.ads_account import AdsAccount
from app.models.campaign import Campaign
from app.models.campaign_product import CampaignProduct
from app.models.ad_group import AdGroup
from app.models.keyword import Keyword
from app.models.target import Target
from app.models.negative_keyword import NegativeKeyword
from app.models.product import Product
from app.schemas.campaign import CampaignCreate
from app.core.exceptions import ServiceValidationError
from app.core.marketplace_rules import today_for, MARKETPLACE_RULES

async def validate_create(db: AsyncSession, account: AdsAccount, rules: dict, payload: CampaignCreate):
    errors = []
    warnings = []
    
    if payload.daily_budget < rules["min_budget"]:
        errors.append({"field": "daily_budget", "message": f"Must be at least {rules['symbol']}{rules['min_budget']}"})
    elif payload.daily_budget < rules["recommended_budget"]:
        warnings.append({"field": "daily_budget", "message": f"Consider at least {rules['symbol']}{rules['recommended_budget']}"})
        
    today = today_for(account.marketplace)
    if payload.start_date < today:
        errors.append({"field": "start_date", "message": "Cannot be in the past"})
        
    # Check name uniqueness (case-insensitive)
    existing_campaign = (await db.execute(select(Campaign).where(
        Campaign.ads_account_id == account.id,
        func.lower(Campaign.name) == payload.name.lower()
    ))).scalars().first()
    if existing_campaign:
        errors.append({"field": "name", "message": "Campaign name already exists"})
        
    # Products validation
    products = (await db.execute(select(Product).where(
        Product.id.in_(payload.product_ids),
        Product.ads_account_id == account.id
    ))).scalars().all()
    
    found_product_ids = {p.id for p in products}
    for pid in payload.product_ids:
        if pid not in found_product_ids:
            errors.append({"field": f"product_ids", "message": f"Product {pid} not found in this account"})
            continue
        p = next(p for p in products if p.id == pid)
        if p.price is None:
            errors.append({"field": f"product_ids", "message": f"Product {pid} must have a price"})
            
    # Check cannibalisation
    if not errors and payload.product_ids:
        cp_res = await db.execute(
            select(CampaignProduct.product_id, Campaign.id)
            .join(Campaign, CampaignProduct.campaign_id == Campaign.id)
            .where(
                CampaignProduct.product_id.in_(payload.product_ids),
                Campaign.targeting_type == payload.targeting_type,
                Campaign.state == "ENABLED"
            )
        )
        cannibals = {}
        for pid, cid in cp_res:
            cannibals.setdefault(pid, []).append(cid)
        if cannibals:
            warnings.append({
                "field": "product_ids", 
                "message": f"Products already advertised in ENABLED {payload.targeting_type} campaigns", 
                "data": cannibals
            })

    ag = payload.ad_groups[0] if payload.ad_groups else None
    if ag:
        if ag.default_bid < rules["min_bid"] or ag.default_bid > rules["max_bid"]:
            errors.append({"field": "ad_groups.0.default_bid", "message": f"Must be between {rules['min_bid']} and {rules['max_bid']}"})
        
        if payload.daily_budget < ag.default_bid * 10:
            warnings.append({"field": "daily_budget", "message": "Budget may be spent after fewer than 10 clicks"})

        if payload.targeting_type == "MANUAL":
            if not ag.keywords and not ag.targets:
                errors.append({"field": "ad_groups.0", "message": "Manual campaigns require at least 1 keyword or product target"})
            if len(ag.keywords) > 0 and len(ag.keywords) < 30:
                warnings.append({"field": "ad_groups.0.keywords", "message": "Amazon recommends at least 30 keywords for manual campaigns"})
        
        elif payload.targeting_type == "AUTO":
            has_enabled_auto = False
            for t in ag.targets:
                if t.kind == "AUTO_GROUP":
                    has_enabled_auto = True
            # In our wizard, AUTO with no targets means "create the 4 auto groups enabled" (we'll do this in write phase)
            # but if they sent some, check if at least one is enabled. Actually, if empty it's fine (we autofill).
            if ag.targets and not has_enabled_auto:
                errors.append({"field": "ad_groups.0.targets", "message": "Auto campaigns require at least 1 enabled auto group"})

        for idx, k in enumerate(ag.keywords):
            bid = k.bid if k.bid is not None else ag.default_bid
            if bid < rules["min_bid"] or bid > rules["max_bid"]:
                errors.append({"field": f"ad_groups.0.keywords.{idx}.bid", "message": f"Must be between {rules['min_bid']} and {rules['max_bid']}"})
                
        for idx, t in enumerate(ag.targets):
            bid = t.bid if t.bid is not None else ag.default_bid
            if bid < rules["min_bid"] or bid > rules["max_bid"]:
                errors.append({"field": f"ad_groups.0.targets.{idx}.bid", "message": f"Must be between {rules['min_bid']} and {rules['max_bid']}"})

        pos_exacts = {k.keyword_text for k in ag.keywords if k.match_type == "EXACT"}
        for idx, nk in enumerate(payload.negative_keywords):
            if nk.match_type == "NEGATIVE_EXACT" and nk.keyword_text in pos_exacts:
                errors.append({"field": f"negative_keywords.{idx}", "message": "Cannot add an exact negative that matches an exact positive keyword"})
                
        # Dedupe negatives
        seen_neg = set()
        deduped_negatives = []
        for nk in payload.negative_keywords:
            key = (nk.keyword_text, nk.match_type)
            if key not in seen_neg:
                seen_neg.add(key)
                deduped_negatives.append(nk)
        payload.negative_keywords = deduped_negatives

    return errors, warnings

async def create_campaign(db: AsyncSession, account: AdsAccount, payload: CampaignCreate):
    rules = MARKETPLACE_RULES[account.marketplace]
    errors, warnings = await validate_create(db, account, rules, payload)
    if errors:
        raise ServiceValidationError(errors)
        
    try:
        # 1. Campaign
        c = Campaign(
            ads_account_id=account.id,
            name=payload.name,
            campaign_type=payload.campaign_type,
            targeting_type=payload.targeting_type,
            state=payload.state,
            start_date=payload.start_date,
            end_date=payload.end_date,
            daily_budget=payload.daily_budget,
            target_acos=payload.target_acos,
            bidding_strategy=payload.bidding_strategy,
            placement_top_pct=payload.placement_top_pct,
            placement_rest_pct=payload.placement_rest_pct,
            placement_product_page_pct=payload.placement_product_page_pct,
            product_id=payload.product_ids[0]
        )
        db.add(c)
        await db.flush()

        # 2. CampaignProducts
        for pid in payload.product_ids:
            cp = CampaignProduct(campaign_id=c.id, product_id=pid)
            db.add(cp)
            
        # 3. AdGroup & Targets/Keywords
        if payload.ad_groups:
            ag_in = payload.ad_groups[0]
            ag = AdGroup(campaign_id=c.id, name=ag_in.name, default_bid=ag_in.default_bid)
            db.add(ag)
            await db.flush()
            
            # Keywords
            for k in ag_in.keywords:
                kw = Keyword(ad_group_id=ag.id, keyword_text=k.keyword_text, match_type=k.match_type, bid=k.bid)
                db.add(kw)
                
            # Targets
            if payload.targeting_type == "AUTO" and not ag_in.targets:
                # AUTO with no targets sent → create the 4 auto groups enabled
                for val in ["CLOSE_MATCH", "LOOSE_MATCH", "SUBSTITUTES", "COMPLEMENTS"]:
                    t = Target(ad_group_id=ag.id, kind="AUTO_GROUP", value=val)
                    db.add(t)
            else:
                for t in ag_in.targets:
                    tg = Target(ad_group_id=ag.id, kind=t.kind, value=t.value, bid=t.bid)
                    db.add(tg)
                    
            # 4. Negatives
            for nk in payload.negative_keywords:
                nkw = NegativeKeyword(campaign_id=c.id, ad_group_id=None, keyword_text=nk.keyword_text, match_type=nk.match_type)
                db.add(nkw)
                
            for asin in payload.negative_asins:
                # Assuming negative ASINs are targets with kind="NEGATIVE_ASIN"
                ntg = Target(ad_group_id=ag.id, kind="NEGATIVE_ASIN", value=asin)
                db.add(ntg)
        
        await db.commit()
    except Exception as e:
        await db.rollback()
        raise e
        
    return await load_detail(db, c.id, warnings)

async def load_detail(db: AsyncSession, campaign_id: int, warnings: list = None):
    # Load campaign details to return
    result = await db.execute(
        select(Campaign).options(
            selectinload(Campaign.product)
        ).where(Campaign.id == campaign_id)
    )
    c = result.scalars().first()
    # To keep response simple and matching CampaignResponse
    res = c.__dict__
    if warnings:
        res["warnings"] = warnings
    return c
